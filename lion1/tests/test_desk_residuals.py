"""The desk's settlement rules: an unread row is never skipped when the chair reads a previous page,
and a lost receipt is never sent again by the desk on its own; a send whose outcome is unknown is
settled by replaying its key, never by the thread's chronology; a budget is charged when a send is
admitted, once per key; two answers in one turn never both take the hour's last slot; and two
discoveries at once settle one send with one row."""

import asyncio
import dataclasses
import json
import re
import types
from datetime import UTC, datetime, timedelta

import pytest

from hub.agent import AgentError, Posture
from hub.agent import desk as desk_module
from hub.agent.chores import Bounded
from hub.agent.desk import Answer, Clarify, Leave, after
from hub.khive import LOST, Khive, KhiveError
from lion1 import Kind, RunRequest
from lion1.notes import FileNoteStore
from tests.test_desk import (
    ASKER,
    DONE,
    LIVE,
    OWNER,
    SELF,
    STEWARD,
    SWEEP,
    FakeKhive,
    FakeReader,
    LostReceipt,
    Probe,
    Scripted,
    build,
    escalations,
    full,
    inbox_row,
    pending,
    sweep_run,
)

TID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"
ARG = re.compile(r"(?:, )?(\w+)=")


def ids_of(n: int) -> tuple[str, str]:
    """The server's ids for the n-th message: `id` is the short form of `full_id`."""
    full_id = f"{n:08x}-aaaa-4bbb-8ccc-{n:012x}"
    return full_id[:8], full_id


class Server(FakeKhive):
    """FakeKhive's keyed comm.send; a message that lands is on its thread as the server lists it (id and
    full_id the full form, short_id apart). `fates[subject]` is spent one per attempt: "refused" is
    refused before it is sent, "before" fails before the server has it, "after" lands and loses the
    answer; with no fate left an attempt lands."""

    def __init__(self, reader: FakeReader, fates: dict[str, list[str]]):
        super().__init__()
        self.reader, self.fates = reader, {k: list(v) for k, v in fates.items()}

    async def send(self, to, content, *, subject=None, thread_id=None, idempotency_key=None):
        fate = self.fates[subject].pop(0) if self.fates.get(subject) else None
        if fate == "refused":
            raise AgentError(f"hop limit: 2 messages already sent on thread {thread_id}; not again")
        if fate == "before":
            raise KhiveError("kkernel did not answer within 60s: the request never reached the server")
        n = len(self.sent)
        sent = await super().send(
            to, content, subject=subject, thread_id=thread_id, idempotency_key=idempotency_key
        )
        if len(self.sent) > n:
            short, full_id = ids_of(len(self.sent))
            row = inbox_row(SELF, subject, content, full_id, thread_id, at=datetime.now(UTC).strftime(STAMP))
            self.reader.threads.setdefault(thread_id, []).append({**row, "short_id": short})
        if fate == "after":
            raise KhiveError(f"kkernel exited 1: {LOST}")
        return sent


class Wire(Khive):
    """`Khive.send` itself: every DSL string it writes is kept, and `server` answers with the server's ids."""

    server: Server

    def __post_init__(self) -> None:
        self.ops: list[str] = []

    async def exec(self, ops: str) -> list:
        self.ops.append(ops)
        assert ops.startswith("comm.send(") and ops.endswith(")"), ops
        args, pos, dec = {}, len("comm.send("), json.JSONDecoder()
        while pos < len(ops) - 1:
            m = ARG.match(ops, pos)
            args[m.group(1)], pos = dec.raw_decode(ops, m.end())
        sent = await self.server.send(args.pop("to"), args.pop("content"), **args)
        short, full_id = ids_of(int(sent["id"][1:]))
        return [{**sent, "id": short, "full_id": full_id}]


def keyed_desk(tmp_path, server: Server, reader: FakeReader):
    wire = Wire(None, SELF)
    wire.server = server
    khive = Bounded(wire, recipients=frozenset({OWNER, STEWARD, SELF}))
    desk, chores, agent = build(tmp_path, khive, reader, [], disk=Probe("disk", full()))
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    run = types.SimpleNamespace(id="r-attempts", record=types.SimpleNamespace(seq=0))
    return desk, agent, wire, run


def question(mid: str, subject: str) -> dict:
    return inbox_row(ASKER, subject, "disk or PR?", mid, TID, at="2026-09-22T10:00:00.000000Z")


def message_of(ref: str, server: Server) -> int | str:
    """Which landed message an id names, in either form; an id naming none stays as it is."""
    return next((n for n in range(1, len(server.sent) + 1) if ref in ids_of(n)), ref)


def sends_by_key(wire: Wire) -> dict:
    """Every comm.send the wire carried, under the key it carried (None: it carried none)."""
    out: dict = {}
    for op in wire.ops:
        m = re.search(r'idempotency_key=("(?:[^"\\]|\\.)*")', op)
        out.setdefault(json.loads(m.group(1)) if m else None, []).append(op)
    return out


def hops(agent) -> dict:
    note = agent.state.get("threads")
    return note.value if note is not None else {}


def charged_keys(agent) -> dict:
    note = agent.state.get("hops_charged")
    return note.value if note is not None else {}


def three_on_one_thread() -> FakeReader:
    return FakeReader(*(question(f"m-{i}", f"q{i}") for i in range(3)))


async def answer_all(desk, run, mids: list[str]) -> list[str]:
    """Answer each message in turn: what each answer said, or what it raised."""
    said = []
    for mid in mids:
        try:
            said.append(await desk.answer(Answer(msg=mid, chore="disk"), run))
        except (KhiveError, AgentError) as e:
            said.append(f"{type(e).__name__}: {e}")
    return said


HOP = f"hop limit: 2 messages already sent on thread {TID}; not again"
M2_REFUSED = f"AgentError: m-2: reply not sent: {HOP}; the message stays listed: answer it again or leave it"


def test_unread_row_is_not_skipped_when_chair_marks_a_previous_page_read(tmp_path):
    rows = [
        inbox_row(
            ASKER, f"q{i}", "fixture", f"m-{i:02d}", f"t-{i:02d}", at=f"2026-09-22T10:00:{i:02d}.000000Z"
        )
        for i in range(21)
    ]

    class ChangingReader(FakeReader):
        changed = False

        async def exec(self, ops):
            response = await super().exec(ops)
            if not self.changed:
                self.changed = True
                self.rows[-1]["status"] = "read"  # chair marks a returned row between page requests
            return response

    reader = ChangingReader(*rows)
    desk, chores, agent = build(tmp_path, FakeKhive(), reader, [])
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    asyncio.run(desk.watch(0))
    first = sorted(pending(agent))
    asyncio.run(desk.watch(0))
    evidence = {
        "first_pending": first,
        "second_pending": sorted(pending(agent)),
        "cursor": agent.state.get("desk_watch").value,
        "oldest_still_unread": rows[0].get("status", "unread"),
        "ops": reader.ops,
    }
    print("EVIDENCE", json.dumps(evidence))
    assert "m-00" in pending(agent), evidence


def test_ambiguous_delivery_is_not_automatically_sent_again_on_the_next_watch(tmp_path):
    class SentButLostReply(FakeKhive):
        first = True

        async def send(self, *args, **kwargs):
            result = await super().send(*args, **kwargs)
            if self.first:
                self.first = False
                raise KhiveError("daemon response lost after request was sent")
            return result

    inner = SentButLostReply()
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    answer = '<lact a1>answer(msg="m-q1", chore="disk")</lact>'
    q = inbox_row(ASKER, "disk?", "how much disk?", "m-q1", "t-q1", at="2026-09-22T10:00:00.000000Z")
    desk, chores, agent = build(
        tmp_path, khive, FakeReader(q), [SWEEP, answer, DONE], disk=Probe("disk", full())
    )
    agent.max_turns = 3
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    first = asyncio.run(agent.wake())
    first_pending = sorted(pending(agent))
    agent.backend.turns = [SWEEP, answer, DONE]
    second = asyncio.run(agent.wake())
    evidence = {
        "deliveries": len(inner.sent),
        "first_outcome": type(first.outcome).__name__,
        "second_outcome": type(second.outcome).__name__ if second else None,
        "first_pending": first_pending,
        "ledger_rows": len(desk.rows()),
        "counted_thread_sends": agent.state.get("threads").value if agent.state.get("threads") else {},
    }
    print("EVIDENCE", json.dumps(evidence))
    assert len(inner.sent) == 1, evidence


def test_a_landed_reply_settles_only_the_obligation_whose_key_it_carries(tmp_path):
    """Short acknowledgement: the first attempt never reaches the
    server; the second lands, acknowledged by its short id while the thread lists full ids. By
    chronology the second's message retired the first as well; by key it settles its own only."""
    reader = FakeReader(question("m-first", "first"), question("m-second", "second"))
    server = Server(reader, {"Re: first": ["before", "before"]})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        with pytest.raises(KhiveError):
            await desk.answer(Answer(msg="m-first", chore="disk"), run)
        await desk.answer(Answer(msg="m-second", chore="disk"), run)
        await desk.watch(0)

    asyncio.run(scenario())
    replies = {r["msg"]: message_of(r["reply"], server) for r in desk.rows()}
    evidence = {"deliveries": len(server.sent), "replies": replies, "pending": sorted(pending(agent))}
    print("EVIDENCE", json.dumps(evidence))
    assert replies == {"m-second": 1}, evidence
    assert "send" in pending(agent)["m-first"] and len(server.sent) == 1, evidence
    by_key = sends_by_key(wire)
    first, second = (f"desk:{SELF}:m-{x}:answered" for x in ("first", "second"))
    assert set(by_key) == {first, second} and len(by_key[first]) == 2, wire.ops
    assert all(len(set(ops)) == 1 for ops in by_key.values()), wire.ops  # the replay is the same bytes


def test_each_unresolved_send_is_settled_by_its_own_key_and_by_no_other_message(tmp_path):
    """Unknown receipt: two sends on one thread are unresolved and only the second
    reached the server. Chronology gave the second's message to the first. Replayed by key, the
    second answers `replayed` with its own message and the first, never received, lands now: one new
    delivery, the first's."""
    reader = FakeReader(question("m-first", "first"), question("m-second", "second"))
    server = Server(reader, {"Re: first": ["before"], "Re: second": ["after"]})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        for mid in ("m-first", "m-second"):
            with pytest.raises(KhiveError):
                await desk.answer(Answer(msg=mid, chore="disk"), run)
        assert [s["subject"] for s in server.sent] == ["Re: second"]
        await desk.watch(0)

    asyncio.run(scenario())
    settled = {r["msg"]: (message_of(r["reply"], server), r.get("settled")) for r in desk.rows()}
    evidence = {
        "sent": [s["subject"] for s in server.sent],
        "settled": settled,
        "pending": sorted(pending(agent)),
    }
    print("EVIDENCE", json.dumps(evidence))
    assert settled.get("m-second") == (1, "replayed"), evidence
    assert [s["subject"] for s in server.sent] == ["Re: second", "Re: first"], (
        evidence
    )  # one new: the first's
    assert settled.get("m-first") == (2, "landed") and pending(agent) == {}, evidence
    by_key = sends_by_key(wire)
    assert sorted(len(ops) for ops in by_key.values()) == [2, 2] and None not in by_key, wire.ops
    assert all(len(set(ops)) == 1 for ops in by_key.values()), wire.ops


@pytest.mark.parametrize("skew", [-120, 120])
def test_an_older_own_message_on_the_thread_never_settles_an_unresolved_send(tmp_path, monkeypatch, skew):
    """Clock skew: a desk clock 120 s behind the server let an own report from before the attempt
    retire it (ahead is the control). No clock is read now: the send stays unresolved until its own key
    settles it."""
    server_now = datetime.now(UTC)

    class DeskClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return server_now + timedelta(seconds=skew)

    monkeypatch.setattr(desk_module, "datetime", DeskClock)
    reader = FakeReader(question("m-first", "first"))
    old = (server_now - timedelta(seconds=60)).strftime(STAMP)
    reader.threads[TID] = [inbox_row(SELF, "an earlier report", "old report", ids_of(99)[1], TID, at=old)]
    server = Server(reader, {"Re: first": ["before", "before"]})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        with pytest.raises(KhiveError):
            await desk.answer(Answer(msg="m-first", chore="disk"), run)
        await desk.watch(0)

    asyncio.run(scenario())
    evidence = {"skew": skew, "rows": desk.rows(), "pending": sorted(pending(agent)), "errors": agent.errors}
    print("EVIDENCE", json.dumps(evidence, default=str))
    assert desk.rows() == [] and "send" in pending(agent)["m-first"] and server.sent == [], evidence


def test_a_keyed_replay_is_the_message_that_landed_and_costs_no_second_hop(tmp_path):
    """The hop count is of messages: a replay answers with the first message and adds none."""
    inner = FakeKhive()
    desk, chores, agent = build(tmp_path, inner, FakeReader(), [])

    def deliver(text: str, key: str) -> dict:
        return asyncio.run(
            agent.deliver(ASKER, text, thread="t-1", run="r-1", subject="s", idempotency_key=key)
        )

    first, again = deliver("x", "k-1"), deliver("x", "k-1")
    assert (first["replayed"], again["replayed"], again["id"]) == (False, True, first["id"])
    assert len(inner.sent) == 1 and agent.state.get("threads").value == {"t-1": 1}
    deliver("y", "k-2")
    assert len(inner.sent) == 2 and agent.state.get("threads").value == {"t-1": 2}
    with pytest.raises(AgentError, match="hop limit: 2 messages already sent on thread t-1"):
        deliver("z", "k-3")


def test_lost_receipts_settled_by_replay_never_carry_a_thread_past_its_hop_cap(tmp_path):
    """Cap 2, three questions on one thread;
    each answer lands and loses its receipt, and discovery settles it by replay before the next. Counted
    only on an answer the server did not call `replayed`, all three went and the thread counted none. The
    hop is held from admission, before the receipt is known."""
    reader = three_on_one_thread()
    server = Server(reader, {f"Re: q{i}": ["after"] for i in range(3)})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        said, unsettled = [], []
        for mid in ("m-0", "m-1", "m-2"):
            said += await answer_all(desk, run, [mid])
            unsettled.append(hops(agent))
            await desk.watch(0)
        return said, unsettled

    said, unsettled = asyncio.run(scenario())
    rows = [(r["msg"], r["class"], r.get("settled")) for r in desk.rows()]
    evidence = {"deliveries": len(server.sent), "threads": hops(agent), "unsettled": unsettled, "said": said}
    evidence.update(rows=rows, pending=sorted(pending(agent)))
    print("EVIDENCE", json.dumps(evidence))
    assert len(server.sent) == 2 and hops(agent) == {TID: 2}, evidence
    assert unsettled == [{TID: 1}, {TID: 2}, {TID: 2}], evidence
    assert said[2] == M2_REFUSED, evidence  # refused with the hop reason: the chair's to leave
    assert rows == [("m-0", "answered", "replayed"), ("m-1", "answered", "replayed")], evidence
    assert "send" not in pending(agent)["m-2"], evidence


def test_unknown_first_attempts_hold_their_hops_so_the_cap_refuses_before_transport(tmp_path):
    """The hop reservation: before any discovery one answer lands and loses its
    receipt and one never reaches the server. Each holds its hop from admission, so at cap 2 the third is
    refused before any transport call, and discovery then settles both within the cap."""
    reader = three_on_one_thread()
    server = Server(reader, {"Re: q0": ["after"], "Re: q1": ["before"]})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        said = await answer_all(desk, run, ["m-0", "m-1", "m-2"])
        before = {"sent": [s["subject"] for s in server.sent], "keys": list(sends_by_key(wire))}
        before["threads"] = hops(agent)
        await desk.watch(0)
        return said, before

    said, before = asyncio.run(scenario())
    rows = [(r["msg"], r["class"], r.get("settled")) for r in desk.rows()]
    evidence = {"said": said, "before_discovery": before, "sent": [s["subject"] for s in server.sent]}
    evidence.update(threads=hops(agent), rows=rows)
    print("EVIDENCE", json.dumps(evidence))
    assert said[2] == M2_REFUSED, evidence
    assert f"desk:{SELF}:m-2:answered" not in before["keys"] and before["threads"] == {TID: 2}, evidence
    assert [s["subject"] for s in server.sent] == ["Re: q0", "Re: q1"] and hops(agent) == {TID: 2}, evidence
    assert rows == [("m-0", "answered", "replayed"), ("m-1", "answered", "landed")], evidence


def test_a_key_is_charged_once_through_a_repeated_settlement_and_a_restart(tmp_path, monkeypatch):
    """Repeat reconciliation and restart: an answer lands and loses its receipt; the first
    settlement's replay comes back `replayed` and its ledger write fails, so the Agent is rebuilt from the
    same notes file and discovery settles the key again. One hop throughout, and a settled key is never
    sent again."""
    reader = FakeReader(question("m-first", "first"))
    server = Server(reader, {"Re: first": ["after"]})
    notes = tmp_path / "notes" / "agent.json"
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)
    agent.state = FileNoteStore(notes)
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    counted = []

    def lost_ledger(*args, **kwargs):
        raise OSError("ledger unavailable after the replay answered")

    async def first_process():
        await desk.sweep(run)
        with pytest.raises(KhiveError):
            await desk.answer(Answer(msg="m-first", chore="disk"), run)
        counted.append(hops(agent))
        monkeypatch.setattr(desk, "_row", lost_ledger)
        with pytest.raises(OSError):
            await desk.watch(0)
        counted.append(hops(agent))

    asyncio.run(first_process())
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)  # the restart: same notes, same ledger
    agent.state = FileNoteStore(notes)

    async def second_process():
        await desk.watch(0)
        counted.append(hops(agent))
        settled_ops = len(wire.ops)
        await desk.watch(0)
        return len(wire.ops) - settled_ops

    again = asyncio.run(second_process())
    rows = [(r["msg"], r["class"], r.get("settled")) for r in desk.rows()]
    evidence = {"counted": counted, "deliveries": len(server.sent), "rows": rows, "sends_after": again}
    evidence["pending"] = sorted(pending(agent))
    print("EVIDENCE", json.dumps(evidence))
    assert counted == [{TID: 1}] * 3, evidence
    assert len(server.sent) == 1 and rows == [("m-first", "answered", "replayed")], evidence
    assert again == 0 and pending(agent) == {}, evidence


def test_a_send_refused_at_the_hop_cap_reserves_nothing_and_is_refused_again(tmp_path):
    """The definite refusal: the hop limit refuses before any charge, so the refused key
    holds no hop; asked again it meets the cap again and never reaches the transport."""
    reader = three_on_one_thread()
    server = Server(reader, {})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        return await answer_all(desk, run, ["m-0", "m-1", "m-2", "m-2"])

    said = asyncio.run(scenario())
    third = f"desk:{SELF}:m-2:answered"
    evidence = {"said": said, "deliveries": len(server.sent), "threads": hops(agent)}
    evidence.update(charged=charged_keys(agent), keys=list(sends_by_key(wire)))
    print("EVIDENCE", json.dumps(evidence))
    assert said[2:] == [M2_REFUSED, M2_REFUSED] and len(server.sent) == 2, evidence
    assert third not in sends_by_key(wire) and third not in charged_keys(agent), evidence
    assert hops(agent) == {TID: 2}, evidence


def test_a_refusal_at_the_transport_gives_back_the_hop_it_was_charged(tmp_path):
    """The hop is charged at admission, before the transport; a transport that refuses before sending (an
    AgentError, as the agent boundary raises; the fake's `refused`) gives it back, as a hop-limit refusal
    takes none. So the two later answers on the thread both go, as when the count followed the send."""
    reader = three_on_one_thread()
    server = Server(reader, {"Re: q0": ["refused"]})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        return await answer_all(desk, run, ["m-0", "m-1", "m-2"])

    said = asyncio.run(scenario())
    evidence = {"said": said, "sent": [s["subject"] for s in server.sent], "threads": hops(agent)}
    evidence["charged"] = charged_keys(agent)
    print("EVIDENCE", json.dumps(evidence))
    assert said[0].startswith("AgentError: m-0: reply not sent:"), evidence
    assert [s["subject"] for s in server.sent] == ["Re: q1", "Re: q2"] and hops(agent) == {TID: 2}, evidence
    assert f"desk:{SELF}:m-0:answered" not in charged_keys(agent), evidence


def two_on_two_threads() -> FakeReader:
    rows = [question("m-first", "first"), question("m-second", "second")]
    for i, row in enumerate(rows):
        row["properties"]["thread_id"] = f"{i:08d}-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    return FakeReader(*rows)


def hour_of(tmp_path, fates: dict[str, list[str]], scenario) -> tuple[list[str], list, dict, dict]:
    """An hourly cap of one over two questions on two threads: what each step said, the ledger as
    (msg, class, why or settled), the evidence, and the keys the wire carried."""
    reader = two_on_two_threads()
    server = Server(reader, fates)
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)
    desk.answers_per_hour = 1
    said = asyncio.run(scenario(desk, run))
    ledger = [(r["msg"], r["class"], r.get("why") or r.get("settled")) for r in desk.rows()]
    evidence = {"said": said, "sent": [s["subject"] for s in server.sent], "ledger": ledger}
    evidence["pending"] = sorted(pending(agent))
    print("EVIDENCE", json.dumps(evidence))
    return said, ledger, evidence, sends_by_key(wire)


CAPPED = "m-second: left, 1 answers already sent this hour"


def test_an_answer_whose_delivery_is_unresolved_holds_its_place_in_the_hourly_cap(tmp_path):
    """Receipt lost before the server: hourly cap 1, two
    questions on two threads, the first send fails before the server. Counted from ledger rows only, the
    second was admitted as well and the next watch delivered both; the unresolved first holds the hour, so
    the second is left `cap` and the watch replays the first alone."""

    async def scenario(desk, run):
        await desk.sweep(run)
        said = await answer_all(desk, run, ["m-first", "m-second"])
        await desk.watch(0)
        return said

    fates = {"Re: first": ["before"], "Re: second": ["before"]}
    said, ledger, evidence, keys = hour_of(tmp_path, fates, scenario)
    assert evidence["sent"] == ["Re: first"] and said[1] == CAPPED, evidence
    assert ledger == [("m-second", "left", "cap"), ("m-first", "answered", "landed")], evidence
    assert f"desk:{SELF}:m-second:answered" not in keys and evidence["pending"] == [], evidence


def test_an_acknowledged_answer_takes_the_hour_and_the_next_is_left_cap(tmp_path):
    """The control, receipt acknowledged: the first answer lands and is acknowledged, the second is
    left `cap`, and the watch has nothing to replay."""

    async def scenario(desk, run):
        await desk.sweep(run)
        said = await answer_all(desk, run, ["m-first", "m-second"])
        await desk.watch(0)
        return said

    said, ledger, evidence, keys = hour_of(tmp_path, {}, scenario)
    assert evidence["sent"] == ["Re: first"] and said[1] == CAPPED, evidence
    assert ledger == [("m-first", "answered", None), ("m-second", "left", "cap")], evidence


def test_a_left_answer_whose_delivery_is_unresolved_still_counts_for_its_hour(tmp_path):
    """The leave: leaving an unresolved answer keeps its send on the row, and a possible
    delivery is a delivery for the budget; this one landed and lost its receipt. The hour stays spent,
    and the next answer is left `cap`."""

    async def scenario(desk, run):
        await desk.sweep(run)
        said = await answer_all(desk, run, ["m-first"])
        said.append(await desk.leave(Leave(msg="m-first", why="notice"), run))
        return said + await answer_all(desk, run, ["m-second"])

    said, ledger, evidence, keys = hour_of(tmp_path, {"Re: first": ["after"]}, scenario)
    assert evidence["sent"] == ["Re: first"] and said[-1] == CAPPED, evidence
    assert ledger == [("m-first", "left", "delivery unresolved"), ("m-second", "left", "cap")], evidence


def test_an_answer_refused_before_its_send_gives_its_hour_back(tmp_path):
    """Hourly control, definite refusal: the first answer is
    refused before its send, its record is cleared and holds no hour, and the second is answered."""

    async def scenario(desk, run):
        await desk.sweep(run)
        return await answer_all(desk, run, ["m-first", "m-second"])

    said, ledger, evidence, keys = hour_of(tmp_path, {"Re: first": ["refused"]}, scenario)
    assert said[0].startswith("AgentError: m-first: reply not sent:"), evidence
    assert evidence["sent"] == ["Re: second"] and ledger == [("m-second", "answered", None)], evidence


def test_a_clarify_is_not_held_to_the_hourly_answer_cap(tmp_path):
    """The hourly cap is of answers: with the hour spent, a clarify on another thread still goes."""

    async def scenario(desk, run):
        await desk.sweep(run)
        said = await answer_all(desk, run, ["m-first"])
        return said + [await desk.clarify(Clarify(msg="m-second", a="disk", b="PR"), run)]

    said, ledger, evidence, keys = hour_of(tmp_path, {}, scenario)
    assert evidence["sent"] == ["Re: first", "Re: second"], evidence
    assert ledger == [("m-first", "answered", None), ("m-second", "clarified", None)], evidence


class OneYieldProbe(Probe):
    """An actor-driven probe instrument: it notes the hourly
    count each answer saw on entry, then yields once, as an instrument awaiting its read does."""

    def __init__(self, desk):
        super().__init__("disk", full())
        self.desk, self.entered = desk, []

    async def run(self, args: dict):
        self.entered.append((args["id"], self.desk._answered_last_hour()))
        await asyncio.sleep(0)
        return await super().run(args)


SAME_TURN = (
    '<lact first>answer(msg="m-first", chore="disk", args={"id": "m-first"})</lact>'
    '<lact second>answer(msg="m-second", chore="disk", args={"id": "m-second"})</lact>'
)


def one_turn_at_cap_one(tmp_path, fates: dict[str, list[str]]) -> dict:
    """Hourly cap 1, two questions on two
    threads; through the actor, turn 1 sweeps, turn 2 answers both, turn 3 ends; then one discovery. Both
    answers are commands of one turn, run as the actor's own tasks. The evidence, before and after it."""
    reader = two_on_two_threads()
    server = Server(reader, fates)
    desk, agent, wire, _ = keyed_desk(tmp_path, server, reader)
    desk.answers_per_hour = 1
    probe = desk.chores.instruments["disk"] = OneYieldProbe(desk)
    # one turn orders two writes (ADR-0005/C6); declared read, two answers overlap as two runs' would
    agent.actor.handlers["answer"] = dataclasses.replace(agent.actor.handlers["answer"], effect="read")
    profile, _ = desk.chores.profiles()
    request = RunRequest(inputs=("tick: sweep",), emits=(Posture,), max_turns=3, accept=desk._accept)

    def seen() -> dict:
        ledger = sorted((r["msg"], r["class"], r.get("why") or r.get("settled")) for r in desk.rows())
        held = sorted(mid for mid, m in pending(agent).items() if m.get("send"))
        return {"sent": [s["subject"] for s in server.sent], "ledger": ledger, "reservations": held}

    async def scenario():
        run = await agent.actor.run(profile, request, Scripted(SWEEP, SAME_TURN, DONE))
        before = seen()
        await desk.watch(0)
        return run, before

    run, before = asyncio.run(scenario())
    results = [
        str(e.content.value if e.content.ok else e.content.error) for e in run.record if e.kind is Kind.RESULT
    ]
    evidence = {"entered": probe.entered, "results": results, "outcome": type(run.outcome).__name__}
    evidence.update(before_discovery=before, **seen(), hourly=desk._answered_last_hour())
    evidence["keys"] = sorted(k for k in sends_by_key(wire) if k)
    print("EVIDENCE", json.dumps(evidence))
    return evidence


def test_two_answers_in_one_turn_never_both_take_the_last_slot_of_the_hour(tmp_path):
    """Acknowledged transport: both answers passed the early check at count 0 and both
    were delivered, so the hour held two answers at cap 1. The final check and the reservation are one
    step now: the first takes the slot, the second is left `cap` and never reaches the transport."""
    ev = one_turn_at_cap_one(tmp_path, {})
    assert ev["entered"] == [("m-first", 0), ("m-second", 0)], ev  # both answers entered their instrument
    assert ev["sent"] == ["Re: first"] and ev["hourly"] == 1, ev
    assert ev["ledger"] == [("m-first", "answered", None), ("m-second", "left", "cap")], ev
    assert CAPPED in ev["results"] and ev["keys"] == [f"desk:{SELF}:m-first:answered"], ev


def test_two_unresolved_answers_in_one_turn_reserve_one_slot_of_the_hour(tmp_path):
    """Both first attempts unavailable: both reservations survived the turn and the next
    discovery first-delivered both at cap 1. One reservation survives now, the other message is left
    `cap`, and discovery delivers the one."""
    ev = one_turn_at_cap_one(tmp_path, {"Re: first": ["before"], "Re: second": ["before"]})
    assert ev["entered"] == [("m-first", 0), ("m-second", 0)], ev
    before = ev["before_discovery"]
    assert before["reservations"] == ["m-first"] and before["sent"] == [], ev
    assert before["ledger"] == [("m-second", "left", "cap")] and CAPPED in ev["results"], ev
    assert ev["sent"] == ["Re: first"] and ev["hourly"] == 1 and ev["reservations"] == [], ev
    assert ev["ledger"] == [("m-first", "answered", "landed"), ("m-second", "left", "cap")], ev
    assert ev["keys"] == [f"desk:{SELF}:m-first:answered"], ev


def clarify_second_after(tmp_path, first_fate: str, leave_first: bool = False) -> tuple:
    """Clarify m-first on a thread (its send's fate chosen), leave it if asked to, then clarify m-second
    on the same thread: what the second clarify said, with the evidence."""
    reader = FakeReader(question("m-first", "first"), question("m-second", "second"))
    server = Server(reader, {"Re: first": [first_fate]})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)

    async def scenario():
        await desk.sweep(run)
        with pytest.raises((KhiveError, AgentError)):
            await desk.clarify(Clarify(msg="m-first", a="disk", b="PR"), run)
        if leave_first:
            await desk.leave(Leave(msg="m-first", why="notice"), run)
        try:
            return await desk.clarify(Clarify(msg="m-second", a="disk", b="PR"), run)
        except AgentError as e:
            return str(e)

    said = asyncio.run(scenario())
    evidence = {"sent": [s["subject"] for s in server.sent], "said": said, "pending": pending(agent)}
    evidence["ledger"] = [(r["msg"], r["class"]) for r in desk.rows()]
    print("EVIDENCE", json.dumps(evidence))
    return said, evidence


ONCE = f"clarify: once per thread; this desk already asked on thread {TID}"


def test_an_unresolved_clarify_holds_the_thread_against_a_second_clarify(tmp_path):
    """The first clarify landed and its receipt was
    lost; a second clarify on the thread was sent as well, because only ledger rows were counted."""
    said, evidence = clarify_second_after(tmp_path, "after")
    assert evidence["sent"] == ["Re: first"], evidence
    assert said == ONCE and "send" in evidence["pending"]["m-first"], evidence


def test_a_left_unresolved_clarify_still_holds_the_thread(tmp_path):
    """Leaving the unresolved clarify writes a `left` row that keeps its send: the thread stays asked."""
    said, evidence = clarify_second_after(tmp_path, "after", leave_first=True)
    assert evidence["sent"] == ["Re: first"], evidence
    assert said == ONCE and evidence["ledger"] == [("m-first", "left")], evidence


def test_a_clarify_refused_before_its_send_holds_nothing_and_the_next_may_ask(tmp_path):
    """Control: a refusal before the send clears the record, so the thread was never asked."""
    said, evidence = clarify_second_after(tmp_path, "refused")
    assert evidence["sent"] == ["Re: second"], evidence
    assert said.startswith(f"m-second: asked {ASKER} on thread {TID}"), evidence
    assert "send" not in evidence["pending"]["m-first"], evidence
    assert evidence["ledger"] == [("m-second", "clarified")], evidence


def test_unresolved_sends_never_take_the_page_a_new_request_needs(tmp_path):
    """Twenty older sends whose outcome is unknown
    filled the default page, so a new request waited behind them for ever as "0 new message(s)"."""
    rows = [question(f"m-old-{i:02d}", f"old request {i}") for i in range(20)]
    for i, row in enumerate(rows):
        row["created_at"] = f"2026-09-22T10:00:{i:02d}.000000Z"
        row["properties"]["thread_id"] = f"{i:08d}-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    reader = FakeReader(*rows)
    server = Server(reader, {f"Re: old request {i}": ["before"] * 9 for i in range(20)})
    desk, agent, wire, run = keyed_desk(tmp_path, server, reader)
    assert desk.page == 20  # the default
    desk.answers_per_hour = len(rows)  # an unresolved answer holds its hour; the cap is not the subject

    async def scenario():
        await desk.sweep(run)
        for old in rows:
            with pytest.raises(KhiveError):
                await desk.answer(Answer(msg=old["id"], chore="disk"), run)
        new = inbox_row(ASKER, "new request", "disk?", "m-new", "t-new", at="2026-09-22T10:01:00.000000Z")
        reader.rows.append(new)
        return await desk.watch(0), await desk.sweep(run)

    notices, listing = asyncio.run(scenario())
    parts = listing.split("\n\n")
    evidence = {"notices": notices, "head": parts[0], "first": parts[1][:60], "parts": len(parts)}
    print("EVIDENCE", json.dumps(evidence))
    assert parts[0].startswith(f"sweep: 1 new message(s) for {OWNER}; answer"), evidence
    assert parts[1].startswith("[msg m-new from lambda:a"), evidence
    assert sum("delivery unresolved" in p for p in parts[2:]) == 20 and len(parts) == 22, evidence
    assert server.sent == [] and desk._accept(run, None) is not None, evidence  # m-new still owes a row


# two discoveries at once (each sweep runs its own): a send settles once, and its budget counts it once

TWO_SWEEPS = "<lact s1>sweep()</lact><lact s2>sweep()</lact>"


def two_sweeps_held(desk, agent, inner: FakeKhive):
    """The real actor's two sweeps of one turn, the transport held until both discoveries have entered,
    never until two sends have: a desk that settles once makes one. The run, and the key of each request
    the transport received (a keyed replay lands no second message, so only this counts it)."""
    requests: list[str] = []
    # one turn orders two writes (ADR-0005/C6); declared read, two sweeps overlap as two runs' would
    agent.actor.handlers["sweep"] = dataclasses.replace(agent.actor.handlers["sweep"], effect="read")

    async def race():
        release, entered, discoveries = asyncio.Event(), asyncio.Event(), 0
        original_discover, original_send = desk._discover, inner.send

        async def discover(*args, **kwargs):
            nonlocal discoveries
            discoveries += 1
            if discoveries == 2:
                entered.set()
            return await original_discover(*args, **kwargs)

        async def send(*args, **kwargs):
            requests.append(kwargs.get("idempotency_key"))
            await release.wait()
            return await original_send(*args, **kwargs)

        desk._discover, inner.send = discover, send
        profile, _ = desk.chores.profiles()
        req = RunRequest(inputs=("tick: sweep",), emits=(Posture,), max_turns=3, accept=desk._accept)
        running = asyncio.create_task(agent.actor.run(profile, req, Scripted(TWO_SWEEPS, DONE)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            release.set()
            return await asyncio.wait_for(running, 2)
        finally:
            release.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            desk._discover, inner.send = original_discover, original_send

    return asyncio.run(race()), requests


def persistent(tmp_path, inner: FakeKhive, reader: FakeReader, **probes: Probe):
    desk, _, agent = build(
        tmp_path, Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF})), reader, [], **probes
    )
    agent.state = FileNoteStore(tmp_path / "state.json")
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    return desk, agent


def test_two_discoveries_at_once_send_one_escalation_and_write_one_row(tmp_path):
    """Two discoveries at once: one escalates the steward's message and its send is in flight
    when the other sees the recorded request and replays it under its key. One send, one `escalated` row,
    and the day's escalations count it once; the send's claim keeps the other from replaying it at all."""
    urgent = inbox_row(STEWARD, "now", "do this now", "m-now", "t-now", at="2026-09-22T10:00:04Z")
    inner, reader = FakeKhive(), FakeReader(urgent)
    reader.threads["t-now"] = [urgent]
    desk, agent = persistent(tmp_path, inner, reader)
    run, requests = two_sweeps_held(desk, agent, inner)
    rows = [(r["msg"], r["class"], r.get("settled")) for r in desk.rows()]
    evidence = {
        "rows": rows,
        "requests": requests,
        "sent": len(inner.sent),
        "pending": sorted(pending(agent)),
    }
    evidence["escalations"] = desk._spent(("escalated",), desk_module._midnight())
    print("EVIDENCE", json.dumps(evidence))
    assert rows == [("m-now", "escalated", None)], evidence
    assert requests == [f"desk:{SELF}:m-now:escalated"] and evidence["sent"] == 1, evidence
    assert evidence["escalations"] == 1 and evidence["pending"] == [], evidence
    assert type(run.outcome).__name__ == "Success", evidence


def test_two_discoveries_replaying_one_lost_answer_write_one_row_and_spend_one_hour(tmp_path):
    """The model-handled class on the same path: an answer landed and lost its receipt, and both
    discoveries of one turn replay its recorded request. One replay, one delivery, one `answered` row, and
    the hour counts it once."""
    inner, reader = LostReceipt(), FakeReader(question("m-q", "q"))
    desk, agent = persistent(tmp_path, inner, reader, disk=Probe("disk", full()))

    async def first_attempt():
        run = sweep_run()
        await desk.sweep(run)
        with pytest.raises(KhiveError):
            await desk.answer(Answer(msg="m-q", chore="disk"), run)

    asyncio.run(first_attempt())
    assert (
        len(inner.sent) == 1
        and desk.rows() == []
        and pending(agent)["m-q"]["send"]["row"]["class"] == "answered"
    )
    run, requests = two_sweeps_held(desk, agent, inner)
    rows = [(r["msg"], r["class"], r.get("settled")) for r in desk.rows()]
    evidence = {
        "rows": rows,
        "requests": requests,
        "sent": len(inner.sent),
        "pending": sorted(pending(agent)),
    }
    evidence["hourly"] = desk._answered_last_hour()
    print("EVIDENCE", json.dumps(evidence))
    assert rows == [("m-q", "answered", "replayed")], evidence
    assert requests == [f"desk:{SELF}:m-q:answered"] and evidence["sent"] == 1, evidence
    assert evidence["hourly"] == 1 and evidence["pending"] == [], evidence
    assert type(run.outcome).__name__ == "Success", evidence


# a discovery that failed to record its request moves no cursor; two discoveries never move it back


class LostIntent:
    """The agent's notes, whose first write of a pending entry carrying `field` (a recorded send or task)
    fails before it persists, as a full disk would; every other write is the store's own."""

    def __init__(self, store, field: str):
        self.store, self.field, self.failed = store, field, 0

    def __getattr__(self, name: str):
        return getattr(self.store, name)

    def put(self, key, value, **meta):
        held = value.values() if key == "desk_pending" and isinstance(value, dict) else ()
        if not self.failed and any(isinstance(m, dict) and m.get(self.field) for m in held):
            self.failed += 1
            raise OSError("no space left on device: the note was not written")
        return self.store.put(key, value, **meta)


def watch_twice(desk, agent) -> tuple[str | None, str | None]:
    """Two watches: what the first raised, if anything, and the cursor it left."""
    try:
        asyncio.run(desk.watch(0))
        raised = None
    except OSError as e:
        raised = f"{type(e).__name__}: {e}"
    cursor = agent.state.get("desk_watch")
    first = (raised, cursor.value if cursor is not None else None)
    asyncio.run(desk.watch(0))
    return first


def test_an_escalation_whose_request_was_never_recorded_is_escalated_by_the_next_discovery(tmp_path):
    """The steward's message is escalated by code; the write that records its request fails before the
    send. Nothing was recorded, so nothing can replay it: the discovery fails before its cursor moves,
    and the next one reads the message again and escalates it, once."""
    urgent = inbox_row(STEWARD, "now", "do this now", "m-now", "t-now", at="2026-09-22T10:00:04Z")
    khive, reader = FakeKhive(), FakeReader(urgent)
    reader.threads["t-now"] = [urgent]
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state = LostIntent(agent.state, "send")
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    raised, cursor = watch_twice(desk, agent)
    evidence = {
        "first_raised": raised,
        "first_cursor": cursor,
        "escalations": escalations(khive),
        "rows": [(r["msg"], r["class"]) for r in desk.rows()],
        "pending": sorted(pending(agent)),
        "errors": agent.errors,
    }
    print("EVIDENCE", json.dumps(evidence))
    assert agent.state.failed == 1, evidence  # the fault was injected
    assert raised is not None and cursor is None, evidence
    assert evidence["escalations"] == [(OWNER, f"[NOW] {STEWARD}: now", None)], evidence
    assert evidence["rows"] == [("m-now", "escalated")] and evidence["pending"] == [], evidence


class FreshVerdict:
    """A verdict notice on the owner's open pull request, at its current head: code files one task."""

    async def triage(self, text: str) -> dict:
        head = "c0ffee00" * 5
        return {
            "errors": [],
            "rungs": {},
            "line": "APPROVE, 0 blocking",
            "state": "OPEN",
            "fresh": True,
            "owner": OWNER,
            "owner_by": "branch",
            "repo": "o/r",
            "number": 7,
            "evaluated_head": head,
            "current_head": head,
            "decision": "APPROVE",
            "blocking": 0,
            "coverage": "every file read",
        }


def test_a_verdict_task_whose_request_was_never_recorded_is_filed_by_the_next_discovery(tmp_path):
    """A verdict notice from a sender in `verdict_from` is filed as a task by code; the write that
    records the task's request fails before the store is asked. The discovery fails before its cursor
    moves, and the next one triages the notice again and files it, once."""
    notice = inbox_row("daemon:review", "verdict o/r#7", "APPROVE", "m-v", "t-v", at="2026-09-22T10:00:04Z")
    inner, reader = FakeKhive(), FakeReader(notice)
    reader.threads["t-v"] = [notice]
    desk, chores, agent = build(
        tmp_path, Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF})), reader, []
    )
    desk.verdicts, desk.verdict_from = FreshVerdict(), ("daemon:review",)
    agent.state = LostIntent(agent.state, "assign")
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    raised, cursor = watch_twice(desk, agent)
    evidence = {
        "first_raised": raised,
        "first_cursor": cursor,
        "tasks": sorted(inner.tasks),
        "rows": [(r["msg"], r["class"]) for r in desk.rows()],
        "pending": sorted(pending(agent)),
        "errors": agent.errors,
    }
    print("EVIDENCE", json.dumps(evidence))
    assert agent.state.failed == 1, evidence
    assert raised is not None and cursor is None, evidence
    assert evidence["tasks"] == [f"desk:{SELF}:m-v:task"], evidence
    assert evidence["rows"] == [("m-v", "tasked")] and evidence["pending"] == [], evidence


def test_a_discovery_that_read_older_mail_and_finished_last_never_moves_the_cursor_back(tmp_path):
    """Two discoveries at once read the box from one cursor: the first is held on its read while the
    second reads a newer message and moves the cursor past it. The first then finishes with an older
    stamp. The cursor stays past the newer one, and both messages are pending."""
    older = inbox_row(ASKER, "q1", "first", "m-old", "t-old", at="2026-09-22T10:00:00.000000Z")
    newer = inbox_row(ASKER, "q2", "second", "m-new", "t-new", at="2026-09-22T10:00:01.000000Z")

    class Overlap(FakeReader):
        calls = 0

        async def exec(self, ops):
            self.calls += 1
            first = self.calls == 1
            self.rows = [older if first else newer]
            page = await super().exec(ops)  # the box as it stood when this read was made
            if first:
                entered.set()
                await release.wait()
            return page

    entered, release = asyncio.Event(), asyncio.Event()
    reader = Overlap()
    desk, chores, agent = build(tmp_path, FakeKhive(), reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)

    async def scenario():
        first = asyncio.create_task(desk._discover())
        await asyncio.wait_for(entered.wait(), 2)
        await desk._discover()
        committed = agent.state.get("desk_watch").value
        release.set()
        await asyncio.wait_for(first, 2)
        return committed

    committed = asyncio.run(scenario())
    evidence = {
        "after_second": committed,
        "after_first": agent.state.get("desk_watch").value,
        "pending": sorted(pending(agent)),
        "reads": reader.calls,
    }
    print("EVIDENCE", json.dumps(evidence))
    assert committed == after(newer["created_at"]) and reader.calls == 2, evidence
    assert evidence["after_first"] == committed, evidence
    assert evidence["pending"] == ["m-new", "m-old"], evidence


# a torn last line of the desk ledger


def test_a_torn_last_ledger_line_is_no_row_and_the_next_row_replaces_it(tmp_path):
    """A crash in the middle of an append leaves the ledger's last line incomplete, with no newline after
    it. That line was never a row: reading skips it, so discovery goes on, and the next row cuts it off
    before it is appended, so it never becomes a middle line."""
    fresh = inbox_row(ASKER, "q", "a question", "m-new", "t-new", at="2026-09-22T10:00:05.000000Z")
    desk, chores, agent = build(tmp_path, FakeKhive(), FakeReader(fresh), [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    good = {"at": "2026-09-22 10:00:00", "msg": "m-good", "from": ASKER, "subject": "s", "thread": "t"}
    good = json.dumps({**good, "class": "left", "run": "-", "why": "notice"})
    torn = '{"at": "2026-09-22 10:00:03", "msg": "m-x", "fr'
    desk.ledger.write_text(good + "\n" + torn)
    asyncio.run(desk.watch(0))
    evidence = {"pending": sorted(pending(agent)), "rows": [r["msg"] for r in desk.rows()]}
    desk._row("m-new", pending(agent)["m-new"], "left", why="notice", run="-")
    evidence["after_row"] = [r["msg"] for r in desk.rows()]
    evidence["text"] = desk.ledger.read_text()
    print("EVIDENCE", json.dumps(evidence))
    assert evidence["pending"] == ["m-new"] and evidence["rows"] == ["m-good"], evidence
    assert evidence["after_row"] == ["m-good", "m-new"] and torn not in evidence["text"], evidence
    assert evidence["text"].endswith("\n") and evidence["text"].count("\n") == 2, evidence


def test_a_bad_middle_ledger_line_fails_closed_and_a_whole_last_row_missing_its_newline_is_kept(tmp_path):
    """Only a torn LAST line is skipped: an undecodable line with a row after it is damage nobody can
    place, and reading refuses. A last row that is whole but lost only its newline is a row, and the next
    row starts on a line of its own."""
    desk, chores, agent = build(tmp_path, FakeKhive(), FakeReader(), [])
    row = {"at": "2026-09-22 10:00:00", "from": ASKER, "subject": "s", "thread": "t", "class": "left"}
    first, second = (json.dumps({**row, "msg": m}) for m in ("m-1", "m-2"))
    desk.ledger.write_text(first + "\n" + '{"at": "2026-09-22 10:00:03", "msg"\n' + second + "\n")
    with pytest.raises(json.JSONDecodeError):
        desk.rows()
    desk.ledger.write_text(first + "\n" + '{"at": "2026-09-22 10:00:03", "msg"')  # the same line, last
    assert [r["msg"] for r in desk.rows()] == ["m-1"]
    desk.ledger.write_text(first)
    assert [r["msg"] for r in desk.rows()] == ["m-1"]
    desk._row("m-2", {k: row[k] for k in ("from", "subject", "thread")}, "left", why="notice", run="-")
    assert [r["msg"] for r in desk.rows()] == ["m-1", "m-2"]
