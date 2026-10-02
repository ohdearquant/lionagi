"""The front desk: a sweep lists the chair's new mail and nothing the desk handled before; an answer
is one instrument's outcome composed by code, sent to the sender on the chair's thread, the chair's
copy never marked; a void answer, a capped hour and an unlisted id are left or refused; mail that
cannot wait (a person's, the steward's, `[NOW]`, a sibling desk's ESCALATE) goes to the chair by code,
verbatim; the reader speaks two read verbs and nothing else."""

import asyncio
import json
import re
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lion_cli.agent import build as cli_build

from hub.agent import Agent, AgentError, Budget, Posture
from hub.agent import desk as desk_module
from hub.agent.chores import Bounded, Chores, Outcome
from hub.agent.desk import READ_VERBS, Desk, Leave, Reader, after
from hub.agent.verdicts import Verdicts
from hub.khive import LOST, Khive, KhiveError
from lionagi import Actor, Kind, RunRequest
from lionagi.notes import FileNoteStore
from tests.test_chores import DONE, OWNER, SELF, STEWARD, FakeKhive, Probe, Scripted, full, mail

ASKER = "lambda:a"  # an agent that wrote to the owner; not in the agent's trusted set


def instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class FakeReader:
    """The chair's inbox as `comm.inbox` answers the desk's reader: `status` (a row is unread unless it
    says otherwise) and `since` (inclusive, compared as instants) filter, rows come newest first, `limit`
    of them from `offset`, with `has_more` and `next_offset` beside them. A row without a stamp passes
    `since`: nothing orders it. `comm.thread(id=)` answers from `threads`; nothing else is answered."""

    def __init__(self, *rows: dict):
        self.rows, self.ops = list(rows), []
        self.threads: dict[str, list[dict]] = {}  # thread id -> the messages `comm.thread` returns

    async def exec(self, ops: str):
        self.ops.append(ops)
        if ops.startswith("comm.thread("):
            tid = re.fullmatch(r'comm\.thread\(id="([^"]*)"\)', ops).group(1)
            msgs = list(self.threads.get(tid, []))
            return [{"thread_id": tid, "count": len(msgs), "messages": msgs}]
        assert ops.startswith("comm.inbox("), ops
        args = dict(re.findall(r'(\w+)=("[^"]*"|\d+)', ops))
        status, since = args.get("status", '"unread"').strip('"'), args.get("since", '""').strip('"')
        limit, offset = int(args.get("limit", 50)), int(args.get("offset", 0))
        rows = [r for r in self.rows if r.get("status", "unread") == status]
        rows = [
            r
            for r in rows
            if not (since and r.get("created_at")) or instant(r["created_at"]) >= instant(since)
        ]
        oldest = datetime.min.replace(tzinfo=UTC)
        rows.sort(key=lambda r: instant(r["created_at"]) if r.get("created_at") else oldest, reverse=True)
        page = rows[offset : offset + limit]
        more = offset + len(page) < len(rows)
        return [
            {
                "messages": page,
                "count": len(page),
                "offset": offset,
                "has_more": more,
                "next_offset": offset + len(page) if more else None,
            }
        ]


def pending(agent: Agent) -> dict:
    note = agent.state.get("desk_pending")
    return note.value if note is not None else {}


def inbox_row(sender: str, subject: str, content: str, mid: str, thread: str, at: str = "") -> dict:
    row = {
        "id": mid,
        "full_id": mid,
        "subject": subject,
        "content": content,
        "properties": {"from_actor": sender, "thread_id": thread},
    }
    if at:
        row["created_at"] = at
    return row


SWEEP = "<lact s1>sweep()</lact>"
Q1 = inbox_row(ASKER, "disk?", "how much disk is free under the roots right now?", "m-q1", "t-q1")
NOTICE = inbox_row("lambda:b", "FYI quota", "weekly at 87%, nothing to do", "m-n1", "t-n1")
PERSON = inbox_row(
    "email:person@example.com",
    "bench?",
    "how is the bench going",
    "m-o1",
    "t-o1",
    at="2026-09-22T10:00:00.000000Z",
)
PERSON_BODY = "how is the bench going? the numbers by six, and the failing cases by name\n-- P"
LIVE = "2026-09-22T09:00:00Z"  # a go-live before every stamped fixture row


def build(
    tmp_path,
    khive: FakeKhive,
    reader: FakeReader,
    turns: list[str],
    cap: int = 10,
    routes: dict[str, str] | None = None,
    admit_replies: bool = False,
    **probes: Probe,
):
    actor = Actor("chores", privileges={"comm.send"})
    agent = Agent(
        actor,
        khive,
        Scripted(*turns),
        lambda mail: profile,
        admit=lambda m: chores.admit(m),
        max_rounds=6,
        budget=Budget(hops_per_thread=2),
    )
    chores = Chores(agent, OWNER, STEWARD, dict(probes), tmp_path / "ledger.jsonl")
    desk = Desk(
        agent,
        chores,
        reader,
        tmp_path / "desk.jsonl",
        answers_per_hour=cap,
        escalate_from=(STEWARD,),
        routes=routes or {},
        admit_replies=admit_replies,
    )
    global profile
    profile, _ = chores.profiles()
    return desk, chores, agent


def results(run) -> list[str]:
    return [
        str(e.content.value if e.content.ok else e.content.error) for e in run.record if e.kind is Kind.RESULT
    ]


def test_a_sweep_lists_the_chairs_new_mail_and_an_answer_replies_on_the_senders_thread_by_code(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    reader = FakeReader(Q1, NOTICE)
    out = full(
        findings=(), evidence="df: 301 GiB free", population="volumes", predicate="under a floor", count=1
    )
    desk, chores, agent = build(
        tmp_path,
        khive,
        reader,
        [
            SWEEP,
            '<lact a1>answer(msg="m-q1", chore="disk")</lact><lact l1>leave(msg="m-n1", why="notice")</lact>',
            DONE,
            SWEEP,
            DONE,
        ],
        disk=Probe("disk", out),
    )
    run = asyncio.run(agent.wake())
    listed = results(run)[0]
    assert listed.startswith(f"sweep: 2 new message(s) for {OWNER}") and "[msg m-q1 from lambda:a" in listed
    assert "how much disk is free" in listed and "[msg m-n1 from lambda:b" in listed
    (sent,) = khive.sent
    assert sent["to"] == ASKER and sent["thread_id"] == "t-q1" and sent["subject"] == "Re: disk?"
    assert sent["content"].startswith(
        f"[desk] answered by {SELF} from its `disk` instrument; it did not mark your message read for {OWNER}"
    )
    assert (
        "answers: what the probe was pointed at" in sent["content"] and "df: 301 GiB free" in sent["content"]
    )
    assert "supersedes: desk" in sent["content"]
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [("m-q1", "answered"), ("m-n1", "left")]
    assert rows[0]["reply"] == "m1" and rows[0]["chore"] == "disk" and rows[1]["why"] == "notice"
    assert any("m-q1: answered lambda:a on thread t-q1 with disk, 0 finding(s)" in r for r in results(run))
    assert all(op.startswith("comm.inbox(") and "since=" in op for op in reader.ops)  # read, never marked
    assert chores.rows() == []  # the desk's answer is not a chore run: no chore ledger row, no owner report
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    run = asyncio.run(agent.wake())  # the same two rows again: both handled, nothing listed
    assert any(f"sweep: nothing new for {OWNER} since" in r for r in results(run)) and len(khive.sent) == 1
    assert (
        desk.summary(rows[0]["at"][:10])
        == f"desk {rows[0]['at'][:10]}: 2 message(s) read, 1 answered, 1 left (notice 1)"
    )


def escalations(khive: FakeKhive) -> list[tuple[str, str, str | None]]:
    """The keyed escalation requests, in send order: (to, subject, the thread the request named)."""
    return [
        (to, subject, thread) for k, ((to, _, subject, thread), _) in khive.keyed.items() if "escalated" in k
    ]


def sweep_run(name: str = "r1") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=name, record=types.SimpleNamespace(seq=0))


def test_a_persons_mail_is_escalated_verbatim_by_code_and_never_reaches_a_model_turn(tmp_path):
    """A person's mail goes to the chair at once, its body read from the thread word for word, by
    code; the sweep never lists it, so an answer or a leave the model names for it is refused."""
    reader = FakeReader(PERSON)
    reader.threads["t-o1"] = [{**PERSON, "content": PERSON_BODY}]
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        reader,
        [
            SWEEP,
            '<lact a1>answer(msg="m-o1", chore="disk")</lact>'
            '<lact l1>leave(msg="m-o1", why="from-person")</lact>',
            DONE,
        ],
        disk=Probe("disk", full()),
    )
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    run = asyncio.run(agent.wake())
    assert escalations(khive) == [(OWNER, "[NOW] email:person@example.com: bench?", None)]
    assert len(khive.sent) == 1 and khive.sent[0]["content"] == (
        "originating msg m-o1 from email:person@example.com on thread t-o1 at 2026-09-22T10:00:00.000000Z\n\n"
        + PERSON_BODY
    )
    assert f"desk:{SELF}:m-o1:escalated" in khive.keyed
    assert [(r["msg"], r["class"], r.get("reply")) for r in desk.rows()] == [("m-o1", "escalated", "m1")]
    assert chores.instruments["disk"].args == [] and pending(agent) == {}  # never run for a person's mail
    said = results(run)
    assert any(r.startswith(f"sweep: nothing new for {OWNER}") for r in said)
    assert sum("desk: 'm-o1' is not a message the sweep listed; known: []" in r for r in said) == 2
    assert 'comm.thread(id="t-o1")' in reader.ops


@pytest.mark.parametrize("subject", ["weekly note", "[NEXT] weekly note", "[FYI] weekly note"])
def test_the_stewards_mail_is_escalated_whatever_its_tag(tmp_path, subject):
    """The red line (a person's mail and the steward's forwarded verbatim, never answered on their
    behalf): a sender in `escalate_from` is escalated by code, and no subject tag lists it to the model."""
    urgent = inbox_row(
        STEWARD, subject, "the lane is yours", "m-l1", "t-l1", at="2026-09-22T10:00:00.000000Z"
    )
    reader = FakeReader(urgent)
    reader.threads["t-l1"] = [urgent]
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    assert asyncio.run(desk.watch(0)) == []
    assert escalations(khive) == [(OWNER, f"[NOW] {STEWARD}: {subject}", None)]
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-l1", "escalated")]
    assert asyncio.run(desk.sweep(sweep_run())).startswith(f"sweep: nothing new for {OWNER}")


def test_now_in_any_senders_subject_is_escalated_and_an_agents_question_is_listed(tmp_path):
    now = inbox_row(
        ASKER, "[NOW] disk full on /a", "writes failing", "m-w1", "t-w1", at="2026-09-22T10:00:01Z"
    )
    ask = inbox_row(ASKER, "disk?", "how much is free?", "m-q1", "t-q1", at="2026-09-22T10:00:02Z")
    reader = FakeReader(now, ask)
    reader.threads["t-w1"] = [now]
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    asyncio.run(desk.watch(0))
    assert escalations(khive) == [(OWNER, f"[NOW] {ASKER}: [NOW] disk full on /a", None)]
    listed = asyncio.run(desk.sweep(sweep_run()))
    assert listed.startswith(f"sweep: 1 new message(s) for {OWNER}") and "[msg m-q1 from" in listed


def test_a_desk_of_this_area_escalating_is_escalated_and_its_other_mail_is_listed(tmp_path):
    """The chores vocabulary (`ESCALATE <chore>: ...`) from a sibling desk of this area; the same subject
    from another area's desk, and the sibling's plain report, wait for the model."""
    stamp = "2026-09-22T10:00:0{}Z".format
    esc = inbox_row(
        f"{OWNER}:mail", "ESCALATE disk: under the floor", "disk3s5 at 280 GiB", "m-e1", "t-e1", stamp(1)
    )
    plain = inbox_row(
        f"{OWNER}:mail", "chore disk: 1 finding(s)", "disk3s5 at 301 GiB", "m-e2", "t-e2", stamp(2)
    )
    other = inbox_row("lambda:a:desk", "ESCALATE disk: x", "theirs", "m-e3", "t-e3", stamp(3))
    reader = FakeReader(esc, plain, other)
    reader.threads["t-e1"] = [esc]
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    asyncio.run(desk.watch(0))
    assert escalations(khive) == [(OWNER, f"[NOW] {OWNER}:mail: ESCALATE disk: under the floor", None)]
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-e1", "escalated")]
    listed = asyncio.run(desk.sweep(sweep_run()))
    assert listed.startswith(f"sweep: 2 new message(s) for {OWNER}")
    assert "[msg m-e2 from" in listed and "[msg m-e3 from" in listed


def test_a_thread_read_that_fails_escalates_the_inbox_snippet_and_says_why(tmp_path):
    class NoThread(FakeReader):
        async def exec(self, ops: str):
            if ops.startswith("comm.thread("):
                raise KhiveError("kkernel exited 1: comm.thread: no such thread")
            return await super().exec(ops)

    head = "originating msg m-o1 from email:person@example.com on thread t-o1 at 2026-09-22T10:00:00.000000Z"
    for reader, why in (
        (NoThread(PERSON), "KhiveError: kkernel exited 1: comm.thread: no such thread"),
        (FakeReader(PERSON), "not among the thread's 0 message(s)"),  # read, and the message is not in it
    ):
        khive = FakeKhive()
        desk, chores, agent = build(tmp_path / why[:5], khive, reader, [])
        agent.state.put("desk_since", LIVE, run="-", seq=0)
        asyncio.run(desk.watch(0))
        assert [s["content"] for s in khive.sent] == [
            f"{head}\n[body: inbox snippet, thread read failed: {why}]\n\nhow is the bench going"
        ]
        assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-o1", "escalated")]


FWD_HEAD = f"originating msg i-1 from {STEWARD} on thread t-1 at 2026-09-22T10:00:00.000000Z"
FWD_BODY = "the lane is yours: rebase on main, then the two failing cases by name\n-- the steward"


def escalated_text(tmp_path, ref: str | None, held: dict) -> str:
    """The escalation of the steward's inbound row i-1 on thread t-1, its `properties.outbound_ref`
    `ref` (none when None), whose thread holds the one message `held`."""
    row = inbox_row(STEWARD, "now", "the inbox snippet", "i-1", "t-1", at="2026-09-22T10:00:00.000000Z")
    if ref is not None:
        row["properties"]["outbound_ref"] = ref
    reader = FakeReader(row)
    reader.threads["t-1"] = [held]
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    asyncio.run(desk.watch(0))
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("i-1", "escalated")]
    (sent,) = khive.sent
    return sent["content"]


def test_a_peers_mail_is_forwarded_with_the_body_its_thread_holds_under_the_outbound_ref(tmp_path):
    """`comm.thread` returns the author-side copies: a peer's message is there under the id its inbox
    row carries as `outbound_ref`, never under the inbound id."""
    text = escalated_text(tmp_path, "o-1", {"id": "o-1", "full_id": "o-1", "content": FWD_BODY})
    assert text == f"{FWD_HEAD}\n\n{FWD_BODY}" and "[body: inbox snippet" not in text


@pytest.mark.parametrize(
    ("ref", "held", "verbatim"),
    [
        ("o-1", "i-1", True),
        ("o-1", "x-9", False),
        (None, "i-1", True),
        (None, "o-1", False),
        (None, None, False),
    ],
    ids=["inbound-id", "neither-id", "no-ref-inbound-id", "no-ref-outbound-copy", "no-ref-idless-message"],
)
def test_the_inbound_id_still_matches_and_a_thread_holding_neither_id_yields_the_snippet(
    tmp_path, ref, held, verbatim
):
    """A row without `outbound_ref` (an older producer) matches by the inbound id only, as before, and
    a thread message without ids matches nothing."""
    text = escalated_text(
        tmp_path, ref, {"content": FWD_BODY} | ({"id": held, "full_id": held} if held else {})
    )
    why = "not among the thread's 1 message(s)"
    snippet = f"{FWD_HEAD}\n[body: inbox snippet, thread read failed: {why}]\n\nthe inbox snippet"
    assert text == (f"{FWD_HEAD}\n\n{FWD_BODY}" if verbatim else snippet)


def test_the_forty_first_escalation_of_a_day_is_left_cap(tmp_path):
    rows = [
        inbox_row(
            "email:person@example.com",
            f"q{i}",
            f"ask {i}",
            f"m-{i:02d}",
            f"t-{i:02d}",
            f"2026-09-22T10:00:{i:02d}Z",
        )
        for i in range(41)
    ]
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, FakeReader(*rows), [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    asyncio.run(desk.watch(0))
    assert len(khive.sent) == 40 and len(escalations(khive)) == 40
    classes = [r["class"] for r in desk.rows()]
    assert classes.count("escalated") == 40 and classes.count("left") == 1
    (capped,) = [r for r in desk.rows() if r["class"] == "left"]
    assert capped["why"] == "cap: escalations" and "chore" not in capped
    assert pending(agent) == {}
    asyncio.run(desk.watch(0))  # the next discovery neither sends it nor lists it
    assert len(khive.sent) == 40 and asyncio.run(desk.sweep(sweep_run())).startswith("sweep: nothing new")


def test_a_lost_escalation_receipt_is_settled_by_its_keyed_replay_and_never_listed(tmp_path):
    """LostReceipt's shape (below) on an escalation: the first attempt lands and loses its answer; while
    the replay fails nothing new is sent and nothing is listed; the replay the server answers `replayed`
    writes the row with the message that landed."""
    inner = LostReceipt()
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    reader = FakeReader(PERSON)
    reader.threads["t-o1"] = [{**PERSON, "content": PERSON_BODY}]
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    assert asyncio.run(desk.watch(0)) == []
    send = pending(agent).get("m-o1", {}).get("send") or {}
    assert len(inner.sent) == 1 and desk.rows() == []
    assert (send.get("to"), send.get("thread"), send.get("key")) == (
        OWNER,
        None,
        f"desk:{SELF}:m-o1:escalated",
    )
    inner.down = True
    assert asyncio.run(desk.watch(0)) == []  # an unresolved escalation wakes nothing
    assert asyncio.run(desk.sweep(sweep_run())).startswith(f"sweep: nothing new for {OWNER}")
    assert agent.errors[-1].startswith("desk: replay of m-o1 unresolved: KhiveError: kkernel did not answer")
    assert len(inner.sent) == 1 and desk.rows() == []
    inner.down = False
    asyncio.run(desk.watch(0))
    rows = [(r["msg"], r["class"], r.get("reply"), r.get("settled")) for r in desk.rows()]
    assert rows == [("m-o1", "escalated", "m1", "replayed")] and pending(agent) == {} and len(inner.sent) == 1


def test_build_reads_the_escalation_rule_from_chores_toml(tmp_path, kkernel_stub):
    chair = tmp_path / "chair"
    (chair / ".khive").mkdir(parents=True)
    (chair / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (tmp_path / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n'
        f"[desk]\nactor_dir = {json.dumps(str(chair))}\n"
        'escalate_from = ["lambda:owner", "lambda:b"]\nescalations_per_day = 5\n'
    )
    desk = cli_build(tmp_path, backend=None)[1].desk
    assert getattr(desk, "escalate_from", None) == ("lambda:owner", "lambda:b")
    assert getattr(desk, "escalations_per_day", None) == 5
    default = chair_agent(tmp_path / "defaults").desk
    assert (getattr(default, "escalate_from", None), getattr(default, "escalations_per_day", None)) == (
        (),
        40,
    )


BUILD = "lambda:x:build"  # a specialized desk of the chair's area
WORK = inbox_row(ASKER, "reindex?", "please rebuild the ADR index", "m-w1", "t-w1", at="2026-09-22T10:00:01Z")
WORK_BODY = "please rebuild the ADR index\nit went stale after 0026 was signed"


def routing(tmp_path, turns: list[str], *rows: dict, cap: int = 10, **probes: Probe):
    """A desk whose `[desk.routes]` names `build`, its client admitting that desk as a recipient; the
    chair's box holds `rows` with WORK's thread readable. Two spare OUTs end a wake whose gate refuses."""
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF, BUILD}))
    reader = FakeReader(*rows)
    reader.threads["t-w1"] = [{**WORK, "content": WORK_BODY}]
    desk, chores, agent = build(tmp_path, khive, reader, turns + [DONE, DONE], cap=cap, **probes)
    desk.routes = {"build": BUILD}
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    return desk, inner, agent


def test_a_route_forwards_the_message_verbatim_to_the_named_desk(tmp_path):
    turns = [SWEEP, '<lact r1>route(msg="m-w1", desk="build")</lact>', DONE]
    desk, inner, agent = routing(tmp_path, turns, WORK)
    run = asyncio.run(agent.wake())
    assert [(s["to"], s["subject"]) for s in inner.sent] == [(BUILD, "route: reindex?")]
    assert inner.sent[0]["content"] == (
        f"originating msg m-w1 from {ASKER} on thread t-w1 at 2026-09-22T10:00:01Z\n\n{WORK_BODY}"
    )
    request, _ = inner.keyed.get(f"desk:{SELF}:m-w1:routed", ((None,) * 4, None))
    assert request[3] is None  # a thread of its own: the named desk is a recipient, no reply grant
    rows = [(r["msg"], r["class"], r.get("to"), r.get("reply")) for r in desk.rows()]
    assert rows == [("m-w1", "routed", BUILD, "m1")] and pending(agent) == {}
    assert any(f"m-w1: routed to {BUILD} (build)" in r for r in results(run))


def test_a_route_to_an_unknown_desk_is_refused_and_the_message_stays_listed(tmp_path):
    turns = [
        SWEEP,
        '<lact r1>route(msg="m-w1", desk="nope")</lact>',
        '<lact l1>leave(msg="m-w1", why="no-instrument")</lact>',
        DONE,
    ]
    desk, inner, agent = routing(tmp_path, turns, WORK)
    run = asyncio.run(agent.wake())
    assert any("route: no desk 'nope'; known: ['build']" in r for r in results(run))
    assert inner.sent == [] and [(r["msg"], r["class"]) for r in desk.rows()] == [("m-w1", "left")]


def test_a_route_forwards_other_departments_mail_only(tmp_path):
    own = inbox_row(OWNER, "reindex", "rebuild it", "m-c1", "t-c1", at="2026-09-22T10:00:02Z")
    sibling = inbox_row(
        f"{OWNER}:mail", "chore disk: quiet", "0 findings", "m-c2", "t-c2", at="2026-09-22T10:00:03Z"
    )
    turns = [
        SWEEP,
        '<lact r1>route(msg="m-c1", desk="build")</lact><lact r2>route(msg="m-c2", desk="build")</lact>',
    ]
    desk, inner, agent = routing(tmp_path, turns + [DONE], own, sibling)
    asyncio.run(agent.wake())
    why = "sender: route forwards other departments' mail only"
    assert inner.sent == []
    assert sorted((r["msg"], r["class"], r.get("why")) for r in desk.rows()) == [
        ("m-c1", "left", why),
        ("m-c2", "left", why),
    ]


@pytest.mark.parametrize("first", ["route", "answer"])
def test_a_route_and_an_answer_share_the_hourly_cap(tmp_path, first):
    """A route counts against `answers_per_hour` as an answer does: whichever goes first takes the hour's
    last slot and the other is left `cap`."""
    ask = inbox_row(ASKER, "disk?", "how much disk is free?", "m-q1", "t-q1", at="2026-09-22T10:00:02Z")
    calls = {
        "route": '<lact r1>route(msg="m-w1", desk="build")</lact>',
        "answer": '<lact a1>answer(msg="m-q1", chore="disk")</lact>',
    }
    second = "answer" if first == "route" else "route"
    turns = [SWEEP, calls[first], calls[second], DONE]
    desk, inner, agent = routing(tmp_path, turns, WORK, ask, cap=1, disk=Probe("disk", full()))
    asyncio.run(agent.wake())
    classes = {r["msg"]: (r["class"], r.get("why")) for r in desk.rows()}
    took, capped = ("m-w1", "m-q1") if first == "route" else ("m-q1", "m-w1")
    assert classes.get(took, (None,))[0] == ("routed" if first == "route" else "answered"), classes
    assert classes.get(capped) == ("left", "cap") and len(inner.sent) == 1, classes


def test_the_guidance_names_the_routed_desks_and_the_task_command(tmp_path):
    _, _, agent = build(tmp_path, FakeKhive(), FakeReader(), [], routes={"build": BUILD})
    profile = agent.profile_for([])
    assert {"route", "task"} <= profile.specs
    assert "route(msg=<id>, desk=<name>)" in profile.system and f"build ({BUILD})" in profile.system
    assert "task(msg=<id>, title=" in profile.system
    _, _, bare = build(tmp_path / "bare", FakeKhive(), FakeReader(), [])
    assert "none, so route nothing" in bare.profile_for([]).system


@pytest.mark.parametrize("actor", ["lambda:a:build", SELF, OWNER])
def test_a_route_outside_the_chairs_area_or_to_itself_is_refused(tmp_path, actor):
    with pytest.raises(ValueError, match=rf"\[desk.routes\] names desks of {OWNER} other than {SELF}"):
        build(tmp_path, FakeKhive(), FakeReader(), [], routes={"elsewhere": actor})


def test_build_admits_the_routed_desks_and_names_them_in_the_guidance(tmp_path, kkernel_stub):
    agent = chair_agent(tmp_path, f'[desk.routes]\nbuild = "{BUILD}"\n')
    assert BUILD in (agent.khive.recipients or ()) and "route" in agent.profile_for([]).specs
    assert getattr(agent.desk, "routes", None) == {"build": BUILD}
    system = agent.profile_for([]).system
    assert "route(msg=<id>, desk=<name>)" in system and f"build ({BUILD})" in system
    assert "none, so route nothing" in chair_agent(tmp_path / "bare").profile_for([]).system


@pytest.mark.parametrize("actor", ["lambda:y:build", "lambda:x:agent", "lambda:x"])
def test_build_refuses_a_route_outside_the_chairs_area_or_to_itself(tmp_path, kkernel_stub, actor):
    with pytest.raises(
        ValueError, match=r"\[desk.routes\] names desks of lambda:x other than lambda:x:agent"
    ):
        chair_agent(tmp_path, f'[desk.routes]\nelsewhere = "{actor}"\n')


@pytest.mark.parametrize("missing", ['from = ["daemon:review"]\n', 'verdict_dir = ""\n'])
def test_build_triages_verdicts_only_from_the_verdict_dir_the_config_names(tmp_path, kkernel_stub, missing):
    """There is no default verdict directory. `[desk.verdicts]` absent: no triage, and no sender's mail is
    read as a verdict notice. Present without a `verdict_dir`: refused at build, naming the key. Present
    with one: the triage reads that directory."""
    desk = chair_agent(tmp_path / "absent").desk
    assert desk.verdicts is None and desk.verdict_from == ()
    with pytest.raises(ValueError, match=r"\[desk.verdicts\] names the verdict_dir"):
        chair_agent(tmp_path / "missing", "[desk.verdicts]\n" + missing)
    verdicts = tmp_path / "verdicts"
    desk = chair_agent(
        tmp_path / "named",
        f'[desk.verdicts]\nverdict_dir = {json.dumps(str(verdicts))}\nfrom = ["daemon:review"]\n',
    ).desk
    assert isinstance(desk.verdicts, Verdicts) and desk.verdicts.dir == str(verdicts)
    assert desk.verdict_from == ("daemon:review",)


TASK = inbox_row(
    ASKER, "index", "please fix the ADR index by friday", "m-t1", "t-t1", at="2026-09-22T10:00:04Z"
)
TASK_BODY = "please fix the ADR index by friday\nit still lists 0026 as a draft"
TASK_CALL = '<lact t1>task(msg="m-t1", title="fix the ADR index")</lact>'
TASK_ID = "7a5c0001-0000-4000-8000-000000000000"  # the fake store's first row


def tasking(tmp_path, turns: list[str], *rows: dict):
    """A desk behind the bounded client over the chair's box `rows`, TASK's thread readable. Two spare
    OUTs end a wake whose gate refuses."""
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    reader = FakeReader(*rows)
    reader.threads["t-t1"] = [{**TASK, "content": TASK_BODY}]
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    desk, chores, agent = build(tmp_path, khive, reader, turns + [DONE, DONE])
    agent.state.put("desk_since", LIVE, run="-", seq=0)
    return desk, inner, agent


def assigns(inner: FakeKhive) -> list[str]:
    return [op for op in inner.ops if op.startswith("gtd.assign(")]


def filed(desk: Desk) -> list[tuple]:
    return [
        (r["msg"], r["class"], r.get("task"), r.get("title"), r.get("replayed"), r.get("why"))
        for r in desk.rows()
    ]


def test_a_task_files_one_row_in_the_chairs_inbox_lane_with_the_message_verbatim(tmp_path):
    """Work-shaped mail becomes one gtd row in the chair's namespace, `inbox` lane, the op
    composed by code with the message word for word and keyed by the message; nothing is sent."""
    desk, inner, agent = tasking(tmp_path, [SWEEP, TASK_CALL, DONE], TASK)
    run = asyncio.run(agent.wake())
    text = f"originating msg m-t1 from {ASKER} on thread t-t1 at 2026-09-22T10:00:04Z\n\n{TASK_BODY}"
    tags = json.dumps(["desk", "from-mail", f"from:{ASKER}"])
    assert assigns(inner) == [
        f'gtd.assign(title="fix the ADR index", namespace="{OWNER}", assignee="{OWNER}", status="inbox", '
        f'priority="p2", tags={tags}, description={json.dumps(text)}, '
        f'idempotency_key="desk:{SELF}:m-t1:task")'
    ]
    assert filed(desk) == [("m-t1", "tasked", TASK_ID, "fix the ADR index", False, None)]
    assert inner.sent == [] and pending(agent) == {}
    assert any(f"m-t1: filed for {OWNER} as task {TASK_ID}" in r for r in results(run))


def test_a_task_whose_answer_was_lost_is_replayed_under_its_key_and_files_one_row(tmp_path):
    """The store wrote the row and its answer was lost: the message stays listed, and the next task on it
    replays the recorded request byte for byte, whatever title the model gives this time, so the store
    answers `replayed` with the same id and the ledger holds one row."""
    turns = [SWEEP, TASK_CALL, '<lact t2>task(msg="m-t1", title="the index again")</lact>', DONE]
    desk, inner, agent = tasking(tmp_path, turns, TASK)
    inner.gtd_lose = 1
    run = asyncio.run(agent.wake())
    ops = assigns(inner)
    assert len(ops) == 2 and ops[0] == ops[1] and len(inner.tasks) == 1, ops
    assert filed(desk) == [("m-t1", "tasked", TASK_ID, "fix the ADR index", True, None)]
    assert any("m-t1: task outcome unknown" in r for r in results(run))


def test_a_message_left_after_a_lost_task_answer_is_left_task_unresolved_with_its_request(tmp_path):
    turns = [SWEEP, TASK_CALL, '<lact l1>leave(msg="m-t1", why="action")</lact>', DONE]
    desk, inner, agent = tasking(tmp_path, turns, TASK)
    inner.gtd_lose = 1
    asyncio.run(agent.wake())
    rows = desk.rows()
    assert [(r["msg"], r["class"], r.get("why")) for r in rows] == [("m-t1", "left", "task unresolved")]
    assert rows[0].get("assign", {}).get("idempotency_key") == f"desk:{SELF}:m-t1:task"


def test_a_task_the_store_refuses_is_left_task_refused_and_never_retried(tmp_path):
    desk, inner, agent = tasking(tmp_path, [SWEEP, TASK_CALL, DONE], TASK)
    inner.gtd_refuse = f"gtd.assign: namespace {OWNER} is not granted to {SELF} for writes; ask its owner"
    asyncio.run(agent.wake())
    assert len(assigns(inner)) == 1 and inner.tasks == {}
    assert filed(desk) == [("m-t1", "left", None, None, None, f"task refused: {inner.gtd_refuse[:80]}")]
    assert pending(agent) == {}


@pytest.mark.parametrize(
    ("title", "files"), [("", False), ("   ", False), ("x" * 121, False), ("x" * 120, True)]
)
def test_a_task_title_is_one_to_120_characters_else_refused_and_the_message_stays_listed(
    tmp_path, title, files
):
    turns = [
        SWEEP,
        f'<lact t1>task(msg="m-t1", title="{title}")</lact>',
        '<lact l1>leave(msg="m-t1", why="action")</lact>',
        DONE,
    ]
    desk, inner, agent = tasking(tmp_path, turns, TASK)
    run = asyncio.run(agent.wake())
    got = [(r["msg"], r["class"]) for r in desk.rows()]
    if files:
        assert got == [("m-t1", "tasked")] and len(assigns(inner)) == 1
    else:
        assert got == [("m-t1", "left")] and assigns(inner) == []
        assert any("task: a title of 1 to 120 characters" in r for r in results(run))


def test_a_task_files_agents_and_daemons_work_the_chairs_own_included(tmp_path):
    own = inbox_row(
        OWNER, "file this", "track the 0026 follow-ups", "m-t2", "t-t2", at="2026-09-22T10:00:05Z"
    )
    bot = inbox_row(
        "daemon:review", "re-review", "re-review PR 12", "m-t3", "t-t3", at="2026-09-22T10:00:06Z"
    )
    anon = inbox_row("local", "do it", "please rebuild", "m-t4", "t-t4", at="2026-09-22T10:00:07Z")
    calls = "".join(
        f'<lact t{m[-1]}>task(msg="{m}", title="work {m}")</lact>' for m in ("m-t2", "m-t3", "m-t4")
    )
    desk, inner, agent = tasking(tmp_path, [SWEEP, calls, DONE], own, bot, anon)
    asyncio.run(agent.wake())
    why = "sender: task files lambda:*, daemon:* senders' work only"
    got = sorted((r["msg"], r["class"], r.get("why")) for r in desk.rows())
    assert got == [("m-t2", "tasked", None), ("m-t3", "tasked", None), ("m-t4", "left", why)]
    assert len(assigns(inner)) == 2


@pytest.mark.parametrize(("today", "want"), [(19, ("tasked", None)), (20, ("left", "cap: tasks"))])
def test_the_twenty_first_task_of_a_day_is_left_cap_tasks(tmp_path, today, want):
    desk, inner, agent = tasking(tmp_path, [SWEEP, TASK_CALL, DONE], TASK)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {"at": now, "msg": f"m-{i:02d}", "from": ASKER, "subject": "s", "thread": f"t-{i:02d}"}
        | {"class": "tasked", "run": "-", "task": f"x{i}", "title": "t"}
        for i in range(today)
    ]
    desk.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    asyncio.run(agent.wake())
    got = [(r["class"], r.get("why")) for r in desk.rows() if r["msg"] == "m-t1"]
    assert got == [want] and len(assigns(inner)) == (1 if want[0] == "tasked" else 0)


def test_build_reads_tasks_per_day_and_the_guidance_names_task(tmp_path, kkernel_stub):
    agent = chair_agent(tmp_path, "tasks_per_day = 7\n")
    assert getattr(agent.desk, "tasks_per_day", None) == 7
    assert getattr(chair_agent(tmp_path / "bare").desk, "tasks_per_day", None) == 20
    profile = agent.profile_for([])
    assert "task" in profile.specs and "task(msg=<id>, title=" in profile.system


class YieldingReader(FakeReader):
    """The chair's box, whose thread read gives the loop a turn as a real read does."""

    async def exec(self, ops: str):
        if ops.startswith("comm.thread"):
            await asyncio.sleep(0)
        return await super().exec(ops)


def over_state(tmp_path, inner: FakeKhive, *rows: dict, body: str = TASK_BODY) -> tuple[Desk, Agent]:
    """A desk over the state file and the ledgers in `tmp_path`; called again, it is the process after a
    restart, and `inner`, the store, keeps what it wrote. TASK's thread holds `body`, the `disk` chore
    answers, and `build` names a desk of the area."""
    reader = YieldingReader(*(rows or (TASK,)))
    reader.threads["t-t1"] = [{**TASK, "content": body}]
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF, BUILD}))
    desk, _, agent = build(tmp_path, khive, reader, [], disk=Probe("disk", full()))
    desk.routes = {"build": BUILD}
    agent.state = FileNoteStore(tmp_path / "state.json")
    if agent.state.get("desk_since") is None:
        agent.state.put("desk_since", LIVE, run="-", seq=0)
    return desk, agent


def run_turns(desk: Desk, agent: Agent, *turns: str):
    """One run of the real actor over `turns`, the sweep's tick its input, OUT gated by the desk."""
    profile, _ = desk.chores.profiles()
    req = RunRequest(inputs=("tick: sweep",), emits=(Posture,), max_rounds=len(turns), accept=desk._accept)
    return asyncio.run(agent.actor.run(profile, req, Scripted(*turns)))


@pytest.mark.parametrize(
    "call",
    [
        '<lact a1>answer(msg="m-t1", chore="disk")</lact>',
        '<lact r1>route(msg="m-t1", desk="build")</lact>',
        '<lact c1>clarify(msg="m-t1", a="the ADR index", b="friday")</lact>',
    ],
    ids=["answer", "route", "clarify"],
)
def test_a_task_whose_outcome_is_unknown_admits_only_its_replay_or_a_leave(tmp_path, call):
    """A task whose row landed and whose answer was lost, the process restarted over the same state: an
    answer, a route or a clarify would retire the message and the recorded request with it, so each is
    refused naming the task and the two calls it admits, nothing is sent, and the message stays listed
    with its request; the replay then files the one row."""
    inner = FakeKhive()
    inner.gtd_lose = 1
    desk, agent = over_state(tmp_path, inner)
    run_turns(desk, agent, SWEEP, TASK_CALL)
    held = pending(agent)["m-t1"]["assign"]
    desk, agent = over_state(tmp_path, inner)
    run = run_turns(desk, agent, SWEEP, call)
    assert inner.sent == [], inner.sent
    refusal = f"m-t1: task unresolved, 'fix the ADR index' under key desk:{SELF}:m-t1:task: task("
    assert any(refusal in r and "or leave(" in r for r in results(run)), results(run)
    assert desk.rows() == [] and pending(agent)["m-t1"]["assign"] == held
    run = run_turns(desk, agent, SWEEP, '<lact t2>task(msg="m-t1", title="the index again")</lact>', DONE)
    ops = assigns(inner)
    assert len(ops) == 2 and ops[0] == ops[1] and len(inner.tasks) == 1, ops
    assert filed(desk) == [("m-t1", "tasked", TASK_ID, "fix the ADR index", True, None)]
    assert inner.sent == [] and pending(agent) == {} and type(run.outcome).__name__ == "Success"


def test_a_task_replayed_after_a_restart_is_the_recorded_request_whatever_the_title_or_body(tmp_path):
    """After a lost answer and a restart, a task with a new title on a thread whose body changed replays
    the recorded request byte for byte under its key: two calls, one row in the store, one ledger row, no
    second thread read."""
    inner = FakeKhive()
    inner.gtd_lose = 1
    desk, agent = over_state(tmp_path, inner)
    run_turns(desk, agent, SWEEP, TASK_CALL)
    held = pending(agent)["m-t1"]["assign"]
    desk, agent = over_state(tmp_path, inner, body="DIFFERENT LATER BODY")
    run = run_turns(desk, agent, SWEEP, '<lact t2>task(msg="m-t1", title="a different title")</lact>', DONE)
    ops = assigns(inner)
    assert len(ops) == 2 and ops[0] == ops[1] and len(inner.tasks) == 1, ops
    assert filed(desk) == [("m-t1", "tasked", TASK_ID, held["title"], True, None)]
    assert not any(op.startswith("comm.thread") for op in desk.reader.ops), desk.reader.ops
    assert pending(agent) == {} and type(run.outcome).__name__ == "Success"
    assert [held[k] for k in ("namespace", "assignee", "status", "priority")] == [OWNER, OWNER, "inbox", "p2"]
    assert held["idempotency_key"] == f"desk:{SELF}:m-t1:task"


def test_a_task_left_unresolved_is_never_tasked_or_answered_after_a_restart(tmp_path):
    """A leave is the disposition: after a restart the left message is tasked and answered by nobody, and
    its row keeps the request."""
    inner = FakeKhive()
    inner.gtd_lose = 1
    desk, agent = over_state(tmp_path, inner)
    run_turns(desk, agent, SWEEP, TASK_CALL, '<lact l1>leave(msg="m-t1", why="action")</lact>', DONE)
    assert filed(desk) == [("m-t1", "left", None, None, None, "task unresolved")]
    assert desk.rows()[0]["assign"]["idempotency_key"] == f"desk:{SELF}:m-t1:task"
    desk, agent = over_state(tmp_path, inner)
    run = run_turns(desk, agent, SWEEP, TASK_CALL + '<lact a1>answer(msg="m-t1", chore="disk")</lact>', DONE)
    assert len(inner.tasks) == 1 and len(assigns(inner)) == 1 and inner.sent == []
    assert len(desk.rows()) == 1 and pending(agent) == {}
    assert sum("not a message the sweep listed" in r for r in results(run)) == 2, results(run)


TASK2 = inbox_row(ASKER, "lock", "please bump the lock file", "m-t2", "t-t2", at="2026-09-22T10:00:05Z")
TASK2_CALL = '<lact t2>task(msg="m-t2", title="bump the lock file")</lact>'


def test_a_task_left_unresolved_keeps_its_slot_of_the_days_budget(tmp_path):
    """A task that may have filed its row holds a slot of the day as a filed one does, pending or left
    `task unresolved`: at one task a day the next is left `cap: tasks`."""
    inner = FakeKhive()
    inner.gtd_lose = 1
    desk, agent = over_state(tmp_path, inner, TASK, TASK2)
    desk.tasks_per_day = 1
    leave = '<lact l1>leave(msg="m-t1", why="action")</lact>'
    run = run_turns(desk, agent, SWEEP, TASK_CALL, leave, TASK2_CALL, DONE)
    assert len(inner.tasks) == 1 and len(assigns(inner)) == 1, assigns(inner)
    assert filed(desk) == [
        ("m-t1", "left", None, None, None, "task unresolved"),
        ("m-t2", "left", None, None, None, "cap: tasks"),
    ]
    assert inner.sent == [] and type(run.outcome).__name__ == "Success"


def test_two_tasks_of_one_turn_share_the_last_slot_of_the_day(tmp_path):
    """Two task commands of one turn, each read and each write giving the loop a turn: the count and the
    record have no await between them, so one files and the other is left `cap: tasks`."""

    class YieldingStore(FakeKhive):
        async def exec(self, ops: str):
            if ops.startswith("gtd.assign"):
                await asyncio.sleep(0)
            return await super().exec(ops)

    inner = YieldingStore()
    desk, agent = over_state(tmp_path, inner, TASK, TASK2)
    desk.tasks_per_day = 1
    run_turns(desk, agent, SWEEP, TASK_CALL + TASK2_CALL, DONE)
    assert len(inner.tasks) == 1 and len(assigns(inner)) == 1, assigns(inner)
    got = sorted((r["class"], r.get("why")) for r in desk.rows())
    assert got == [("left", "cap: tasks"), ("tasked", None)], got


def test_a_refused_task_holds_no_slot_and_the_next_task_of_the_day_is_filed(tmp_path):
    """A refusal filed nothing, so it holds nothing of the day: at one task a day another message is
    filed after it, and the refused one is never retried."""
    inner = FakeKhive()
    inner.gtd_refuse = f"gtd.assign: namespace {OWNER} is not granted to {SELF} for writes"
    desk, agent = over_state(tmp_path, inner, TASK, TASK2)
    desk.tasks_per_day = 1
    run_turns(desk, agent, SWEEP, TASK_CALL)
    assert inner.tasks == {} and desk.rows()[0]["why"].startswith("task refused:")
    inner.gtd_refuse = ""
    run_turns(desk, agent, SWEEP, TASK2_CALL, DONE)
    assert len(inner.tasks) == 1 and len(assigns(inner)) == 2
    assert [r[:2] for r in filed(desk)] == [("m-t1", "left"), ("m-t2", "tasked")]


T0 = 1_790_000_000  # a fixed instant the brief tests count from (2026-09-21, no DST change near it)


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def stored(agent: Agent, key: str):
    note = agent.state.get(key)
    return note.value if note is not None else None


def ledger_row(t: float, mid: str, cls: str, **extra) -> dict:
    """A desk ledger row as `_row` writes it, stamped at `t` in local time and in UTC."""
    at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    row = {"at": at, "utc": iso(t), "msg": mid, "from": ASKER, "subject": f"s {mid}", "thread": f"t-{mid}"}
    return {**row, "class": cls, "run": "-", **extra}


def briefing(tmp_path, *rows: dict):
    """A desk live since T0 whose ledger holds `rows`; the test sets the agent's clock."""
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, FakeReader(), [])
    agent.state.put("desk_since", iso(T0), run="-", seq=0)
    desk.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return desk, chores, khive, agent


FOLD = (1414906200.0, 1414909800.0)  # 05:30Z and 06:30Z on 2014-11-02: both 01:30 in US Eastern time


@pytest.fixture
def fold_clock(monkeypatch):
    """The desk's clock under a zone whose DST change repeats 01:00 to 02:00; the test moves `now`."""
    clock = types.SimpleNamespace(now=FOLD[0])

    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return clock.now

        def strftime(self, fmt, t=None):
            return time.strftime(fmt, time.localtime(clock.now) if t is None else t)

    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0/2,M11.1.0/2")
    time.tzset()
    monkeypatch.setattr(desk_module, "time", Clock())
    try:
        yield clock
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("consumer", ["brief", "cap"])
def test_a_ledger_row_counts_at_its_instant_when_a_dst_change_repeats_its_local_hour(
    tmp_path, fold_clock, consumer
):
    """Two rows an hour apart carry one local label, 01:30, on the night the clocks go back. A brief
    whose window opens between them briefs only the second, and a cap cut between them counts only the
    second: each row is read at its own instant, never at its local label."""
    early, late = FOLD
    labels = {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) for t in FOLD}
    assert labels == {"2014-11-02 01:30:00"}  # the zone applied: one label, two instants
    desk, chores, khive, agent = briefing(tmp_path)
    agent.clock = lambda: fold_clock.now
    agent.state.put("desk_since", iso(early - 3600), run="-", seq=0)
    seen = {"from": ASKER, "subject": "s", "thread": "t"}
    desk._row("m-early", seen, "answered", run="-", chore="disk", findings=["f"])
    fold_clock.now = late
    desk._row("m-late", seen, "answered", run="-", chore="disk", findings=["f"])
    fold_clock.now = late + 900
    if consumer == "brief":
        agent.state.put("desk_brief_at", iso(early + 900), run="-", seq=0)  # a brief delivered between
        text = "\n".join(desk.digest_lines("-"))
        assert "m-late" in text and "m-early" not in text, text
    else:
        assert desk._spent(("answered",), early + 1800) == 1, desk.rows()


def test_a_ledger_row_counts_at_its_instant_to_the_microsecond(tmp_path, fold_clock):
    """A cut half a second into the second a row was written in: the row, written after the cut,
    counts, since its stamp keeps the fraction."""
    _, late = FOLD
    desk, chores, khive, agent = briefing(tmp_path)
    agent.clock = lambda: fold_clock.now
    fold_clock.now = late + 0.75
    desk._row("m-frac", {"from": ASKER, "subject": "s", "thread": "t"}, "answered", run="-", chore="disk")
    assert desk._spent(("answered",), late + 0.5) == 1, desk.rows()
    assert desk._spent(("answered",), late + 0.8) == 0, desk.rows()


def test_the_second_brief_starts_where_the_first_ended_and_each_row_is_briefed_once(tmp_path):
    """The brief covers the window since the last one delivered, floored to the second as
    the ledger's stamps are; a row stamped after the first brief's end is the second brief's alone."""
    desk, chores, khive, agent = briefing(
        tmp_path, ledger_row(T0 + 10, "m-a1", "answered", chore="disk", findings=["301 GiB free"])
    )
    agent.clock = lambda: T0 + 20.7
    first = asyncio.run(chores.digest(None, sweep_run("d1")))
    with desk.ledger.open("a") as fh:
        fh.write(json.dumps(ledger_row(T0 + 20, "m-b1", "left", why="notice")) + "\n")
    agent.clock = lambda: T0 + 40
    second = asyncio.run(chores.digest(None, sweep_run("d2")))
    assert f"front desk {iso(T0)} to {iso(T0 + 20)}: 0 sweep(s), 1 message(s) read, 1 answered," in first
    assert "desk answered m-a1 from" in first and "m-b1" not in first
    assert (
        f"front desk {iso(T0 + 20)} to {iso(T0 + 40)}: 0 sweep(s), 1 message(s) read, 0 answered, "
        "0 routed, 0 tasked, 0 escalated, 1 left, 0 asked; nothing anomalous"
    ) in second
    assert f"left notice: m-b1 from {ASKER} 's m-b1'" in second and "m-a1" not in second
    assert [s["to"] for s in khive.sent] == [OWNER, OWNER] and stored(agent, "desk_brief_at") == iso(T0 + 40)


def test_a_clean_window_briefs_the_liveness_line_with_the_sweeps_run_and_then_starts_over(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(tmp_path, khive, FakeReader(), [SWEEP, DONE])
    agent.state.put("desk_since", iso(T0), run="-", seq=0)
    asyncio.run(agent.wake())
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    agent.backend.turns = [SWEEP, DONE]
    asyncio.run(agent.wake())
    t = time.time() + 5
    agent.clock = lambda: t
    assert desk.digest_lines("any day") == [
        f"front desk {iso(T0)} to {iso(t)}: 2 sweep(s), 0 message(s) read, 0 answered, 0 routed, "
        "0 tasked, 0 escalated, 0 left, 0 asked; nothing anomalous"
    ]
    asyncio.run(chores.digest(None, sweep_run("d1")))  # delivered: the sweeps it counted are spent
    agent.clock = lambda: t + 60
    assert desk.digest_lines("any day")[0].startswith(f"front desk {iso(t)} to {iso(t + 60)}: 0 sweep(s),")


def test_an_escalation_in_the_window_makes_the_brief_anomalous_and_names_it(tmp_path):
    person = {"from": "email:person@example.com"}
    desk, chores, khive, agent = briefing(
        tmp_path, ledger_row(T0 + 5, "m-e1", "escalated", reply="m9", **person)
    )
    agent.clock = lambda: T0 + 60
    assert desk.digest_lines("any day") == [
        f"front desk {iso(T0)} to {iso(T0 + 60)}: 0 sweep(s), 1 message(s) read, 0 answered, 0 routed, "
        "0 tasked, 1 escalated, 0 left, 0 asked; "
        "ANOMALOUS: 1 escalated, 0 delivery unresolved, 0 clarify open, 0 refused",
        "desk escalated m-e1 from email:person@example.com 's m-e1'",
    ]


def test_the_brief_names_routed_and_tasked_rows_and_lists_the_left_ones_grouped_by_why(tmp_path):
    rows = (
        ledger_row(T0 + 1, "m-l1", "left", why="notice"),
        ledger_row(T0 + 2, "m-r1", "routed", to="lambda:x:build", reply="m7"),
        ledger_row(T0 + 3, "m-l2", "left", why="cap"),
        ledger_row(T0 + 4, "m-t1", "tasked", task="7a5c0001-0000-4000-8000-000000000000", title="fix it"),
        ledger_row(T0 + 5, "m-l3", "left", why="notice"),
        ledger_row(T0 + 6, "m-l4", "left", why="task refused: gtd.assign: not granted"),
    )
    desk, chores, khive, agent = briefing(tmp_path, *rows)
    agent.clock = lambda: T0 + 60
    assert desk.digest_lines("any day") == [
        f"front desk {iso(T0)} to {iso(T0 + 60)}: 0 sweep(s), 6 message(s) read, 0 answered, 1 routed, "
        "1 tasked, 0 escalated, 4 left, 0 asked; "
        "ANOMALOUS: 0 escalated, 0 delivery unresolved, 0 clarify open, 2 refused",
        f"desk routed m-r1 from {ASKER} 's m-r1' to lambda:x:build",
        f"desk tasked m-t1 from {ASKER} 's m-t1' as 7a5c0001-0000-4000-8000-000000000000 'fix it'",
        f"left cap: m-l2 from {ASKER} 's m-l2'",
        f"left notice: m-l1 from {ASKER} 's m-l1'",
        f"left notice: m-l3 from {ASKER} 's m-l3'",
        f"left task refused: gtd.assign: not granted: m-l4 from {ASKER} 's m-l4'",
    ]


def test_a_brief_past_sixty_lines_says_how_many_more_the_ledger_holds(tmp_path):
    rows = [ledger_row(T0 + i, f"m-{i:02d}", "left", why="notice") for i in range(65)]
    desk, chores, khive, agent = briefing(tmp_path, *rows)
    agent.clock = lambda: T0 + 100
    lines = desk.digest_lines("any day")
    assert len(lines) == 62 and lines[0].startswith(f"front desk {iso(T0)} to {iso(T0 + 100)}: 0 sweep(s)")
    assert lines[60] == f"left notice: m-59 from {ASKER} 's m-59'" and lines[61] == "[5 more in the ledger]"


def test_a_brief_whose_delivery_failed_is_briefed_again_from_the_same_start(tmp_path):
    desk, chores, khive, agent = briefing(tmp_path, ledger_row(T0 + 5, "m-a1", "left", why="notice"))
    agent.clock = lambda: T0 + 20
    khive.refuse.add(OWNER)
    with pytest.raises(AgentError):
        asyncio.run(chores.digest(None, sweep_run("d1")))
    khive.refuse.clear()
    agent.clock = lambda: T0 + 30
    text = asyncio.run(chores.digest(None, sweep_run("d2")))
    assert f"front desk {iso(T0)} to {iso(T0 + 30)}: 0 sweep(s), 1 message(s) read," in text
    assert f"left notice: m-a1 from {ASKER} 's m-a1'" in text and stored(agent, "desk_brief_at") == iso(
        T0 + 30
    )


@pytest.mark.parametrize("refused", [False, True], ids=["delivered", "refused"])
def test_two_digests_of_one_turn_brief_each_row_and_each_sweep_once(tmp_path, refused):
    """Two digest commands of one turn: the second renders its window only once the first is delivered
    and committed, so a row stamped while the first is on the wire is briefed exactly once, by the second
    or, when the second is refused before it is sent, by the next brief; no row and no sweep is briefed
    twice."""
    now = [T0 + 20]

    class Wire(FakeKhive):
        n = 0

        async def send(self, *args, **kwargs):
            self.n += 1
            if self.n == 1:  # the first brief is on the wire: a row lands and the clock moves on
                with desk.ledger.open("a") as fh:
                    fh.write(json.dumps(ledger_row(T0 + 25, "m-b1", "left", why="notice")) + "\n")
                now[0] = T0 + 30
                await asyncio.sleep(0)
            elif self.n == 2 and refused:
                raise AgentError("refused before it was sent")
            return await super().send(*args, **kwargs)

    khive = Wire(mail(SELF, "tick: digest"))
    turns = ["<lact d1>digest()</lact><lact d2>digest()</lact>", DONE]
    desk, chores, agent = build(tmp_path, khive, FakeReader(), turns)
    agent.state.put("desk_since", iso(T0), run="-", seq=0)
    agent.state.put("desk_sweeps", 1, run="-", seq=0)
    desk.ledger.write_text(json.dumps(ledger_row(T0 + 5, "m-a1", "left", why="notice")) + "\n")
    agent.clock = lambda: now[0]
    asyncio.run(agent.wake())
    now[0] = T0 + 40
    asyncio.run(chores.digest(None, sweep_run("d3")))
    texts = [s["content"] for s in khive.sent]
    assert len(texts) == (2 if refused else 3), texts
    assert [sum(f"{mid} from" in t for t in texts) for mid in ("m-a1", "m-b1")] == [1, 1], texts
    assert sum(int(re.search(r"(\d+) sweep\(s\)", t).group(1)) for t in texts) == 1, texts


def test_two_digests_in_sequence_in_one_wake_brief_a_row_once(tmp_path):
    """Sequential briefs in one wake."""
    khive = FakeKhive(mail(SELF, "tick: digest"))
    turns = ["<lact d1>digest()</lact>", "<lact d2>digest()</lact>", DONE]
    desk, chores, agent = build(tmp_path, khive, FakeReader(), turns)
    agent.state.put("desk_since", iso(T0), run="-", seq=0)
    desk.ledger.write_text(json.dumps(ledger_row(T0 + 5, "m-a1", "left", why="notice")) + "\n")
    agent.clock = lambda: T0 + 20
    asyncio.run(agent.wake())
    assert len(khive.sent) == 2 and "m-a1 from" in khive.sent[0]["content"]
    assert "m-a1 from" not in khive.sent[1]["content"]


@pytest.mark.parametrize("lost", [False, True], ids=["refused", "lost"])
def test_a_brief_refused_or_lost_moves_neither_its_window_nor_its_sweeps(tmp_path, lost):
    """A refusal, or a delivery whose receipt was lost, commits nothing: the next brief repeats the
    window and the sweep count."""
    desk, chores, khive, agent = briefing(tmp_path, ledger_row(T0 + 5, "m-a1", "left", why="notice"))
    agent.clock = lambda: T0 + 20
    agent.state.put("desk_sweeps", 2, run="-", seq=0)
    send = khive.send

    async def fault(*args, **kwargs):
        if lost:
            await send(*args, **kwargs)
            raise KhiveError(LOST)
        raise AgentError("refused before it was sent")

    khive.send = fault
    with pytest.raises((AgentError, KhiveError)):
        asyncio.run(chores.digest(None, sweep_run("d1")))
    assert stored(agent, "desk_brief_at") is None and stored(agent, "desk_sweeps") == 2
    khive.send = send
    agent.clock = lambda: T0 + 30
    text = asyncio.run(chores.digest(None, sweep_run("d2")))
    assert "m-a1 from" in text and f"front desk {iso(T0)} to {iso(T0 + 30)}: 2 sweep(s)," in text
    assert stored(agent, "desk_brief_at") == iso(T0 + 30) and stored(agent, "desk_sweeps") == 0


@pytest.mark.parametrize(("extra", "repeat"), [("", "every:12h"), ('brief = "every:6h"\n', "every:6h")])
def test_the_brief_is_the_digest_tick_repeating_as_desk_brief_says(
    tmp_path, kkernel_stub, monkeypatch, extra, repeat
):
    ops: list[str] = []

    async def recorded(self, op):
        ops.append(op)
        return [{"events": []}] if op.startswith("schedule.agenda") else [{"ok": True}]

    monkeypatch.setattr(Khive, "exec", recorded)
    agent = chair_agent(tmp_path, extra)
    asyncio.run(agent.desk.chores.ensure_ticks())
    reminds = [op for op in ops if op.startswith("schedule.remind(") and 'content="tick: digest"' in op]
    assert len(reminds) == 1 and f'repeat="{repeat}"' in reminds[0], ops


def test_a_desk_less_agent_keeps_the_daily_digest(tmp_path, kkernel_stub):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (tmp_path / "chores.toml").write_text('actor = "lambda:x:agent"\nowner = "lambda:x"\n')
    chores, agent = cli_build(tmp_path, backend=None)
    assert getattr(chores, "digest_every", "daily") == "daily" and getattr(agent, "desk", None) is None


def test_a_void_answer_is_left_with_the_instruments_own_reason_and_nothing_is_sent(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1),
        [SWEEP, '<lact a1>answer(msg="m-q1", chore="disk")</lact>', DONE],
        disk=Probe("disk", Outcome(evidence="df: garbage", control=False)),
    )
    run = asyncio.run(agent.wake())
    assert khive.sent == []
    (row,) = desk.rows()
    assert row["class"] == "left" and row["why"].startswith(
        "instrument: the instrument's control did not pass"
    )
    assert any("m-q1: left, the instrument did not answer" in r for r in results(run))


def test_the_hourly_cap_leaves_the_next_answer_and_the_reply_grant_is_the_senders_own_message(tmp_path):
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    q2 = inbox_row(ASKER, "pr?", "what state are the PRs in", "m-q2", "t-q2")
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1, q2),
        [
            SWEEP,
            '<lact a1>answer(msg="m-q1", chore="disk")</lact><lact a2>answer(msg="m-q2", chore="pr")</lact>',
            DONE,
        ],
        cap=1,
        disk=Probe("disk", full()),
        pr=Probe("pr", full()),
    )
    with pytest.raises(AgentError, match="lambda:a is not a recipient"):
        asyncio.run(khive.send(ASKER, "x"))  # before any sweep: an outsider
    asyncio.run(agent.wake())
    assert [s["to"] for s in inner.sent] == [ASKER]  # granted by the message the chair received
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [("m-q1", "answered"), ("m-q2", "left")]
    assert rows[1]["why"] == "cap"


def test_reply_authority_is_one_message_one_thread(tmp_path):
    """ADR-0017 C3: an answer is one send to the listed message's sender on its thread,
    spent by that send; a Check's asker and thread are a message the wake admitted, refused before the
    instrument runs or anything is sent."""
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1),
        [SWEEP, '<lact a1>answer(msg="m-q1", chore="disk")</lact>', DONE],
        disk=Probe("disk", full()),
    )
    asyncio.run(agent.wake())
    assert [(s["to"], s["thread_id"]) for s in inner.sent] == [(ASKER, "t-q1")]  # the answer itself
    probe = chores.instruments["disk"]
    ran = len(probe.args)
    for thread in ("t-q1", "t-other", None):  # the answered message's reply is spent; no thread has one
        try:
            asyncio.run(khive.send(ASKER, "again", thread_id=thread))
        except AgentError:
            pass
    inner.mail.append(mail(OWNER, "check disk please", mid="b2c3d4e5"))
    agent.backend.turns = [
        f'<lact c1>check(chore="disk", asker="{OWNER}", thread="t-fabricated")</lact>'
        f'<lact c2>check(chore="disk", asker="{STEWARD}")</lact>',
        DONE,
    ]
    run = asyncio.run(agent.wake())
    extra = [(s["to"], s["thread_id"]) for s in inner.sent[1:]]
    assert (extra, probe.args[ran:], chores.rows()) == ([], [], [])
    said = results(run)
    assert any(f"{OWNER} on thread t-fabricated is not a message this wake admitted" in r for r in said)
    assert any(f"{STEWARD} on thread None is not a message this wake admitted" in r for r in said)


def test_a_second_message_on_the_same_thread_gets_its_own_reply(tmp_path):
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    again = inbox_row(ASKER, "disk again?", "and how much now?", "m-q1b", "t-q1")
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1, again),
        [
            SWEEP,
            '<lact a1>answer(msg="m-q1", chore="disk")</lact>'
            '<lact a2>answer(msg="m-q1b", chore="disk")</lact>',
            DONE,
        ],
        disk=Probe("disk", full()),
    )
    asyncio.run(agent.wake())
    assert [(s["to"], s["thread_id"]) for s in inner.sent] == [(ASKER, "t-q1"), (ASKER, "t-q1")]
    assert sorted((r["msg"], r["class"]) for r in desk.rows()) == [
        ("m-q1", "answered"),
        ("m-q1b", "answered"),
    ]


def test_a_refused_reply_spends_only_its_own_attempt():
    """A definite failure does not bar the pair: the message's next attempt holds a reply of its own."""
    inner = FakeKhive()
    inner.refuse.add(ASKER)
    khive = Bounded(inner, recipients=frozenset({OWNER}))

    async def attempt():
        with khive.reply(ASKER, "t-q1"):
            return await khive.send(ASKER, "x", thread_id="t-q1")

    with pytest.raises(AgentError, match="hop limit"):
        asyncio.run(attempt())
    inner.refuse.clear()
    assert asyncio.run(attempt())["thread_id"] == "t-q1"
    with pytest.raises(AgentError, match="the message names none"):
        with khive.reply(ASKER, None):
            pass


def test_build_wires_the_desk_from_chores_toml_with_a_reader_that_only_reads(tmp_path, kkernel_stub):
    chair = tmp_path / "chair"
    (chair / ".khive").mkdir(parents=True)
    (chair / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (tmp_path / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n'
        '[chores.disk]\nevery = "every:2h"\nroots = ["/"]\n'
        f'[desk]\nactor_dir = {json.dumps(str(chair))}\nevery = "every:5m"\n'
        "answers_per_hour = 3\nclarify_hours = 12\nadmit_replies = true\n"
    )
    chores, agent = cli_build(tmp_path, backend=None)
    assert (
        chores.extra_specs == frozenset({"sweep", "answer", "leave", "clarify", "route", "task"})
        and chores.extra_ticks == {}
    )
    assert agent.watch == agent.desk.watch  # the chair's box wakes the desk; no tick is scheduled
    assert agent.profile_for([]).specs == frozenset(
        {"check", "digest", "aged", "sweep", "answer", "leave", "clarify", "route", "task"}
    )
    assert "You are also the front desk of lambda:x" in agent.profile_for([]).system
    desk = agent.desk
    assert desk.answers_per_hour == 3 and desk.clarify_hours == 12 and desk.admit_replies is True
    assert desk.reader.allowed == READ_VERBS and desk.reader.actor == "lambda:x" and desk.reader.cwd == chair
    assert not any(hasattr(desk.reader, n) for n in ("send", "grant", "mark_read", "inbox"))
    with pytest.raises(AgentError, match=r"\['comm.read'\] are not verbs"):
        asyncio.run(desk.reader.exec('comm.read(ids=["x"])'))
    assert Path(desk.ledger) == tmp_path / "desk.jsonl"
    # the setting is on for the boolean alone: a quoted "true" or "false" is a typo, and reads as off
    toml = tmp_path / "chores.toml"
    toml.write_text(toml.read_text().replace("admit_replies = true", 'admit_replies = "true"'))
    assert cli_build(tmp_path, backend=None)[1].desk.admit_replies is False


def chair_agent(tmp_path, extra: str = "") -> Agent:
    """`lion agent`'s own build over an agent directory whose desk reads the chair `lambda:x`; `extra` is
    appended to its chores.toml."""
    chair = tmp_path / "chair"
    (chair / ".khive").mkdir(parents=True)
    (chair / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (tmp_path / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n'
        f"[desk]\nactor_dir = {json.dumps(str(chair))}\n" + extra
    )
    return cli_build(tmp_path, backend=None)[1]


def test_reader_helpers_cannot_write(tmp_path, kkernel_stub, monkeypatch):
    """The reader `lion agent` builds reaches kkernel as the chair through its admitted `exec` only:
    no helper of the chair's client marks, sends or is granted a recipient. A helper that is not there
    is a refusal too."""
    reached: list[tuple[str, str]] = []

    async def recorded(self, ops):
        reached.append((self.actor, ops))
        return [{"messages": []}]

    monkeypatch.setattr(Khive, "exec", recorded)
    reader = chair_agent(tmp_path).desk.reader
    for name, args in (
        ("mark_read", (["m-q1"],)),
        ("grant", ("lambda:a",)),
        ("send", ("lambda:a", "as the chair")),
        ("inbox", ()),
    ):
        try:
            out = getattr(reader, name)(*args)
            if asyncio.iscoroutine(out):
                asyncio.run(out)
        except (AttributeError, AgentError):
            pass
    refused = []
    for ops in (
        'comm.read(ids=["m-q1"])',
        '{"tool": "comm.read", "args": {"ids": ["m-q1"]}}',
        # four JSON-form ops a client refuses, through the reader this time
        '{"tool": "comm.send", "args": {"to": "email:someone@example.com", "content": "hi"}}',
        '[{"tool": "gtd.complete", "args": {"id": "00000000-0000-0000-0000-000000000000"}}]',
        '{"tool": "memory.remember", "args": {"content": "x", "salience": 0.99}}',
        '{"tool": "delete", "args": {"id": "x"}}',
    ):
        try:
            asyncio.run(reader.exec(ops))
        except AgentError as e:
            refused.append(str(e).startswith("agent boundary"))
    assert reached == [], f"kkernel was handed {reached} through the reader"
    assert refused == [True] * 6
    asyncio.run(reader.exec('comm.inbox(status="unread", limit=1)'))  # the recorder is live
    assert reached == [("lambda:x", 'comm.inbox(status="unread", limit=1)')]


def test_agent_marks_only_own_mail(tmp_path):
    """The agent's own mail is marked read before its gate, admitted or refused; the chair's rows the
    desk read are never marked, and the reader never sees `comm.read`."""

    class Marking(FakeKhive):
        def __init__(self, *inbox):
            super().__init__(*inbox)
            self.marked: list[str] = []

        async def mark_read(self, ids):
            self.marked += ids

    khive = Marking(mail(SELF, "tick: sweep"), mail("lambda:stranger", "hi", mid="b2c3d4e5"))
    reader = FakeReader(Q1)
    desk, chores, agent = build(
        tmp_path, khive, reader, [SWEEP, '<lact l1>leave(msg="m-q1", why="notice")</lact>', DONE]
    )
    assert asyncio.run(agent.wake()) is not None
    assert khive.marked == [
        "a1b2c3d4-0000-0000-0000-000000000000",
        "b2c3d4e5-0000-0000-0000-000000000000",
    ]
    assert agent.state.get("cursor").value["dropped"] == [{"id": "b2c3d4e5", "from": "lambda:stranger"}]
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-q1", "left")]
    assert not any("comm.read" in op for op in reader.ops) and reader.ops


def test_out_waits_while_a_listed_message_is_neither_answered_nor_left(tmp_path):
    """A model can write OUT{} before it has routed what its sweep listed. The gate refuses that OUT
    with the ids, judged on the record as it stands: a leave beside the OUT has not landed, so both ids
    are named, and the refusal settles the leave; the next OUT names the one still listed."""
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1, NOTICE),
        [
            SWEEP,
            '<lact l1>leave(msg="m-n1", why="notice")</lact> ' + DONE,  # both still listed when judged
            DONE,  # one taken, one still listed
            '<lact l2>leave(msg="m-q1", why="no-instrument")</lact>',
            DONE,
            SWEEP,
            DONE,
        ],
    )
    run = asyncio.run(agent.wake())
    assert type(run.outcome).__name__ == "Success" and len(run.record) == 15  # 5 rounds, the closure
    retries = [str(e.content) for e in run.record if e.kind is Kind.SYSTEM]
    assert any(
        "listed 2 message(s) you have not answered or left: ['m-n1', 'm-q1']; call answer(" in str(r)
        for r in retries
    )
    assert any("listed 1 message(s) you have not answered or left: ['m-q1']" in str(r) for r in retries)
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-n1", "left"), ("m-q1", "left")]
    assert khive.sent == []
    # a wake whose sweep listed nothing, and a wake with no sweep at all, end on their first OUT
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    run = asyncio.run(agent.wake())
    assert type(run.outcome).__name__ == "Success" and len(run.record) == 7  # 2 rounds, no retry


CQ = inbox_row(ASKER, "disk or pr?", "is it the disk under the roots or PR 12 that is red?", "m-c1", "t-c1")


def test_a_clarify_quotes_the_message_asks_once_and_is_resolved_by_the_next_message_on_the_thread(tmp_path):
    reader = FakeReader(CQ)
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        reader,
        [
            SWEEP,
            '<lact c1>clarify(msg="m-c1", a="the disk under the roots", b="PR 12 that is red")</lact>',
            DONE,
            SWEEP,
            DONE,
            SWEEP,
            '<lact l1>leave(msg="m-c2", why="no-instrument")</lact>',
            DONE,
        ],
    )
    run = asyncio.run(agent.wake())
    (sent,) = khive.sent
    assert sent["to"] == ASKER and sent["thread_id"] == "t-c1" and sent["subject"] == "Re: disk or pr?"
    assert sent["content"].startswith(
        f"[desk clarify] {SELF} could not tell whether you mean 'the disk under the roots' or "
        f"'PR 12 that is red'; say which in a message to {OWNER} on this thread"
    )
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [("m-c1", "clarified")] and rows[0]["reply"] == "m1"
    assert any("m-c1: asked lambda:a on thread t-c1 whether" in r for r in results(run))
    day = rows[0]["at"][:10]
    t = time.time() + 2  # the brief's window ends at the clock's second; the clarify's row is inside it
    agent.clock = lambda: t
    assert desk.digest_lines(day) == [
        f"front desk {stored(agent, 'desk_since')} to {iso(t)}: 1 sweep(s), 1 message(s) read, 0 answered, "
        "0 routed, 0 tasked, 0 escalated, 0 left, 1 asked; "
        "ANOMALOUS: 0 escalated, 0 delivery unresolved, 1 clarify open, 0 refused",
        "desk asked lambda:a on thread t-c1 'disk or pr?': 'the disk under the roots' or 'PR 12 that is red'",
        f"desk clarify open since {rows[0]['at']}: m-c1 from lambda:a 'disk or pr?'",
    ]
    assert desk.summary(day) == f"desk {day}: 1 message(s) read, 0 answered, 0 left, 1 asked"  # --stats
    agent.clock = time.time
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    run = asyncio.run(agent.wake())  # nothing new: the clarified message is not listed again
    assert any(f"sweep: nothing new for {OWNER}" in r for r in results(run))
    reader.rows.append(inbox_row(ASKER, "Re: disk or pr?", "the disk", "m-c2", "t-c1"))
    khive.mail.append(mail(SELF, "tick: sweep", mid="c3d4e5f6"))
    asyncio.run(agent.wake())
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [
        ("m-c1", "clarified"),
        ("m-c2", "left"),
        ("m-c1", "resolved"),
    ]
    assert rows[-1]["by"] == "m-c2" and desk._open_clarifies() == {} and len(khive.sent) == 1


def test_the_model_is_told_the_readings_the_validator_admits():
    """The clarify spec and the guidance name the same three sources the validator accepts."""
    from hub import guidance

    fields = desk_module.Clarify.model_fields
    assert "a chore's name or a leave test" in fields["a"].description
    assert "a chore's name or a leave test" in " ".join(desk_module.Clarify.__doc__.split())
    assert "a chore's name or one of the leave tests" in guidance("desk")


def test_a_clarify_is_refused_unless_both_readings_are_quoted_and_agents_only_once_per_thread(tmp_path):
    of_clarify = inbox_row(
        "lambda:b:desk",
        "Re: x",
        "[desk clarify] lambda:b:desk could not tell whether you mean 'a' or 'b'",
        "m-c4",
        "t-c4",
    )
    # not an agent, not a person's mail (which is escalated before any sweep): the anonymous actor
    anonymous = inbox_row("local", "which?", "the disk or PR 12", "m-c5", "t-c5")
    reader = FakeReader(CQ, of_clarify, anonymous)
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        reader,
        [
            SWEEP,
            '<lact c1>clarify(msg="m-c1", a="the disk", b="the moon")</lact>'
            '<lact c2>clarify(msg="m-c1", a="the disk", b="The Disk")</lact>'
            '<lact c3>clarify(msg="m-c1", a="the disk under the roots", b="PR 12")</lact>'
            '<lact c4>clarify(msg="m-c4", a="a", b="b")</lact>'
            '<lact c5>clarify(msg="m-c5", a="the disk", b="PR 12")</lact>',
            '<lact l4>leave(msg="m-c4", why="notice")</lact>',
            DONE,
            SWEEP,
            '<lact c6>clarify(msg="m-c3", a="disk", b="PR 12")</lact>'
            '<lact l3>leave(msg="m-c3", why="no-instrument")</lact>',
            DONE,
        ],
    )
    run = asyncio.run(agent.wake())
    out = results(run)
    assert any("clarify: 'the moon' is not in the message; quote its words or name a chore" in r for r in out)
    assert any("clarify: the two readings are the same words" in r for r in out)
    assert any("clarify: m-c4 is itself a clarify: answer or leave it, never clarify" in r for r in out)
    (sent,) = khive.sent
    assert sent["to"] == ASKER and "'the disk under the roots' or 'PR 12'" in sent["content"]
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [
        ("m-c1", "clarified"),
        ("m-c5", "left"),
        ("m-c4", "left"),
    ]
    assert rows[1]["why"] == "sender: clarify asks lambda:*, daemon:* senders only"
    reader.rows.append(inbox_row(ASKER, "again", "disk or PR 12?", "m-c3", "t-c1"))
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    run = asyncio.run(agent.wake())
    assert any("clarify: once per thread; this desk already asked on thread t-c1" in r for r in results(run))
    assert len(khive.sent) == 1
    assert [(r["msg"], r["class"]) for r in desk.rows()][3:] == [("m-c3", "left"), ("m-c1", "resolved")]


def test_an_unanswered_clarify_expires_into_a_leave_the_chair_hears_of(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(CQ),
        [
            SWEEP,
            '<lact c1>clarify(msg="m-c1", a="the disk under the roots", b="PR 12")</lact>',
            DONE,
            SWEEP,
            DONE,
        ],
    )
    asyncio.run(agent.wake())
    desk.clarify_hours = -1  # the cap is already behind us: the next sweep leaves it
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    run = asyncio.run(agent.wake())
    assert any("1 clarify unanswered past -1h, left: ['m-c1']" in r for r in results(run))
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [("m-c1", "clarified"), ("m-c1", "left")]
    assert rows[1]["why"] == "clarify unanswered" and desk._open_clarifies() == {} and len(khive.sent) == 1
    day = rows[0]["at"][:10]
    assert (
        desk.summary(day)
        == f"desk {day}: 1 message(s) read, 0 answered, 1 left (clarify unanswered 1), 1 asked"
    )


ANSWER = "d4e5f6a7"  # a clarify's answer as the desk's own box holds it
ANSWER_ID = f"{ANSWER}-0000-0000-0000-000000000000"


def clarified(tmp_path, turns: list[str], admit_replies: bool = True, **probes: Probe):
    """A desk that clarified m-c1 on thread t-c1 in its first wake; `turns` are the model's after that."""
    reader, khive = FakeReader(CQ), FakeKhive(mail(SELF, "tick: sweep"))
    ask = '<lact c1>clarify(msg="m-c1", a="the disk under the roots", b="PR 12")</lact>'
    desk, chores, agent = build(
        tmp_path, khive, reader, [SWEEP, ask, DONE, *turns], admit_replies=admit_replies, **probes
    )
    asyncio.run(agent.wake())
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-c1", "clarified")]
    return desk, khive, reader, agent


def test_a_clarifys_answer_mailed_to_the_desk_is_admitted_and_listed_under_the_clarified_message(tmp_path):
    out = full(findings=(), evidence="df: 301 GiB free", population="volumes", predicate="under a floor")
    answer = f'<lact a1>answer(msg="{ANSWER_ID}", chore="disk")</lact>'
    desk, khive, reader, agent = clarified(tmp_path, [SWEEP, answer, DONE], disk=Probe("disk", out))
    khive.mail.append(mail(ASKER, "the disk under the roots", mid=ANSWER, thread="t-c1"))
    run = asyncio.run(agent.wake())
    assert run is not None and "dropped" not in stored(agent, "cursor")
    assert results(run)[0].endswith(
        f"[msg {ANSWER_ID} from {ASKER} subject 'disk or pr?']\n"
        "is it the disk under the roots or PR 12 that is red?\n"
        "[the answer to the desk's clarify, 'the disk under the roots' or 'PR 12']\n"
        "the disk under the roots"
    )
    sent = khive.sent[-1]
    assert len(khive.sent) == 2 and "df: 301 GiB free" in sent["content"]
    assert sent["to"] == ASKER and sent["thread_id"] == "t-c1" and sent["subject"] == "Re: disk or pr?"
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [
        ("m-c1", "clarified"),
        (ANSWER_ID, "answered"),
        ("m-c1", "resolved"),
    ]
    assert rows[-1]["by"] == ANSWER_ID and desk._open_clarifies() == {} and pending(agent) == {}


@pytest.mark.parametrize(
    "sender, mid, to_desk",
    [(ASKER, "m-c2", False), ("lambda:b", "m-c3", False), (ASKER, ANSWER_ID, True)],
    ids=["to-the-chair", "another-sender-on-the-thread", "to-the-desk"],
)
def test_a_clarify_whose_answer_is_pending_at_the_cap_is_resolved_by_it_and_never_left_unanswered(
    tmp_path, sender, mid, to_desk
):
    """An answer received and not yet handled is not an unanswered clarify: the sweep past the cap lists
    it and leaves nothing, and handling it resolves the clarify. The thread is the association, as in
    the resolution."""
    leave = f'<lact l1>leave(msg="{mid}", why="no-instrument")</lact>'
    desk, khive, reader, agent = clarified(tmp_path, [SWEEP, leave, DONE], admit_replies=to_desk)
    if to_desk:  # the gate admits it into the intake
        khive.mail.append(mail(sender, "the disk under the roots", mid=ANSWER, thread="t-c1"))
    else:
        reader.rows.append(inbox_row(sender, "Re: disk or pr?", "the disk", mid, "t-c1"))
        khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    desk.clarify_hours = -1  # the cap is already behind us
    run = asyncio.run(agent.wake())
    said = results(run)[0]
    assert f"[msg {mid} from {sender}" in said and "unanswered" not in said
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [
        ("m-c1", "clarified"),
        (mid, "left"),
        ("m-c1", "resolved"),
    ]
    assert rows[-1]["by"] == mid and desk._open_clarifies() == {} and pending(agent) == {}


def test_a_message_pending_on_another_thread_does_not_hold_a_clarify_past_the_cap(tmp_path):
    desk, khive, reader, agent = clarified(tmp_path, [])
    reader.rows.append(inbox_row(ASKER, "Re: disk or pr?", "the disk", "m-c2", "t-other"))
    desk.clarify_hours = -1
    said = asyncio.run(desk.sweep(sweep_run()))
    assert "1 clarify unanswered past -1h, left: ['m-c1']" in said and f"[msg m-c2 from {ASKER}" in said
    rows = desk.rows()
    assert [(r["msg"], r["class"], r.get("why")) for r in rows[1:]] == [
        ("m-c1", "left", "clarify unanswered")
    ]
    assert list(pending(agent)) == ["m-c2"] and desk._open_clarifies() == {}


def fyi(i: int) -> dict:
    """A notice on a thread of its own, stamped `i` seconds past ten."""
    return inbox_row(
        "lambda:b", f"n{i}", "fyi", f"m-n{i:02d}", f"t-n{i:02d}", at=f"2026-09-22T10:00:{i:02d}Z"
    )


def test_a_sweep_whose_read_paged_past_a_clarifys_answer_leaves_no_clarify(tmp_path):
    """Offsets index the unread set, which the chair changes between pages: the chair reads the newest
    row after the first page, the second page starts one row late, and the answer, the oldest row
    stamped, is never read. That sweep leaves no clarify past the cap; the next lists the answer, and
    leaving it resolves the clarify."""
    desk, khive, reader, agent = clarified(tmp_path, [])
    agent.state.put("desk_watch", LIVE, run="-", seq=0)
    newer = [fyi(i) for i in range(1, 21)]
    answer = inbox_row(ASKER, "Re: disk or pr?", "the disk", "m-c2", "t-c1", at="2026-09-22T10:00:00Z")
    reader.rows += [*newer, answer]
    read = reader.exec

    async def chair_reads_between_pages(ops: str):
        page = await read(ops)
        newer[-1].setdefault("status", "read")
        return page

    reader.exec = chair_reads_between_pages
    desk.clarify_hours = -1  # the cap is already behind us
    said = asyncio.run(desk.sweep(sweep_run("r1")))
    assert "m-c2" not in pending(agent) and "unanswered" not in said
    assert list(desk._open_clarifies()) == ["m-c1"]
    run = sweep_run("r2")
    said = asyncio.run(desk.sweep(run))
    assert f"[msg m-c2 from {ASKER}" in said and "unanswered" not in said
    asyncio.run(desk.leave(Leave(msg="m-c2", why="no-instrument"), run))
    rows = desk.rows()
    assert [(r["msg"], r["class"]) for r in rows] == [
        ("m-c1", "clarified"),
        ("m-c2", "left"),
        ("m-c1", "resolved"),
    ]
    assert rows[-1]["by"] == "m-c2" and desk._open_clarifies() == {}


def test_a_clarify_past_the_cap_is_left_by_the_first_sweep_whose_read_fits_one_page(tmp_path):
    """Twenty notices and the clarified message, unread, are two pages: that sweep leaves nothing. The
    chair reads one, the next read fits a page, and with nothing pending on its thread the clarify is
    left as unanswered."""
    desk, khive, reader, agent = clarified(tmp_path, [])
    agent.state.put("desk_watch", LIVE, run="-", seq=0)
    newer = [fyi(i) for i in range(1, 21)]
    reader.rows += newer
    desk.clarify_hours = -1
    said = asyncio.run(desk.sweep(sweep_run("r1")))
    assert "unanswered" not in said and list(desk._open_clarifies()) == ["m-c1"]
    newer[-1]["status"] = "read"
    said = asyncio.run(desk.sweep(sweep_run("r2")))
    assert "1 clarify unanswered past -1h, left: ['m-c1']" in said and desk._open_clarifies() == {}


@pytest.mark.parametrize(
    "sender, thread, close, admit_replies",
    [
        ("lambda:b", "t-c1", False, True),
        (ASKER, "t-other", False, True),
        (ASKER, "t-c1", True, True),
        (ASKER, "t-c1", False, False),
    ],
    ids=["another-actor", "another-thread", "a-closed-clarify", "the-setting-off"],
)
def test_mail_to_the_desk_that_answers_no_open_clarify_of_its_sender_is_dropped_at_the_gate(
    tmp_path, sender, thread, close, admit_replies
):
    leave = '<lact l1>leave(msg="m-c2", why="no-instrument")</lact>'
    desk, khive, reader, agent = clarified(tmp_path, [SWEEP, leave, DONE], admit_replies=admit_replies)
    if close:  # the answer to the chair, handled, resolves the clarify
        reader.rows.append(inbox_row(ASKER, "Re: disk or pr?", "the disk", "m-c2", "t-c1"))
        khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
        asyncio.run(agent.wake())
        assert desk._open_clarifies() == {}
    khive.mail.append(mail(sender, "the disk under the roots", mid=ANSWER, thread=thread))
    assert asyncio.run(agent.wake()) is None
    assert stored(agent, "cursor")["dropped"] == [{"id": ANSWER, "from": sender}]
    assert pending(agent) == {} and len(khive.sent) == 1


LISTED = "an answer mailed to you is listed there under the message it clarifies, by the answer's own id"


@pytest.mark.parametrize("admit_replies", [True, False], ids=["the-setting-on", "the-setting-off"])
def test_the_guidance_names_the_listing_of_an_answer_only_when_the_setting_admits_one(
    tmp_path, admit_replies
):
    desk, khive, reader, agent = clarified(tmp_path, [DONE], admit_replies=admit_replies)
    text = desk.chores.extra_system
    assert (LISTED in text) is admit_replies
    assert (
        "$replies" not in text and "in a later sweep" + ("; an answer" if admit_replies else ". Once") in text
    )


def test_the_gate_reads_the_ledger_once_per_message(tmp_path):
    desk, khive, reader, agent = clarified(tmp_path, [DONE])
    reads: list[str] = []
    rows = desk.rows
    desk.rows = lambda: reads.append("read") or rows()
    answer = mail(ASKER, "the disk under the roots", mid=ANSWER, thread="t-c1")
    assert desk.admit(answer) is True and reads == ["read"]  # an answer: the open clarifies, then its own row
    reads.clear()
    assert desk.admit(mail("lambda:b", "not an answer", mid="b2c3d4e5", thread="t-c1")) is False
    assert reads == ["read"]


def test_a_ledger_the_gate_cannot_read_leaves_the_mail_to_the_agents_gate_and_the_error_on_the_wake(tmp_path):
    desk, khive, reader, agent = clarified(tmp_path, [DONE])
    text = desk.ledger.read_text()
    desk.ledger.write_text(text + "{not json\n" + text)  # an undecodable line in the middle, not a torn tail
    with pytest.raises(json.JSONDecodeError):  # the read itself fails
        desk.rows()
    khive.mail.append(mail(OWNER, "status please", mid="a1b2c3d4", thread="t-own"))
    khive.mail.append(mail(ASKER, "the disk under the roots", mid=ANSWER, thread="t-c1"))
    run = asyncio.run(agent.wake())
    assert run is not None and stored(agent, "taking") == {}  # the owner's mail ran
    assert stored(agent, "cursor")["dropped"] == [{"id": ANSWER, "from": ASKER}]  # the answer met C3's gate
    assert [e for e in agent.errors if e.startswith("desk gate failed: JSONDecodeError")] != []
    assert (
        len([e for e in agent.errors if e.startswith("desk gate failed")]) == 1
    )  # once, whatever the passes


def test_a_row_the_chair_has_not_handled_wakes_the_desk_and_a_handled_one_moves_the_cursor_only(tmp_path):
    """No tick: the desk long-polls the chair's box beside its own. The first wake here has no own
    mail; the watch finds Q1, the wake sweeps and answers it; the next poll reads from the saved cursor,
    finds nothing past it, and there is no wake and no model call. A row the chair already read is never
    listed."""
    q1 = inbox_row(ASKER, "disk?", "how much disk is free?", "m-q1", "t-q1", at="2026-09-22T21:59:17.849963Z")
    seen = inbox_row(ASKER, "old", "the chair read this one", "m-r", "t-r", at="2026-09-22T21:59:18.000000Z")
    seen["status"] = "read"
    khive = FakeKhive()  # the desk's own box stays empty throughout
    reader = FakeReader(q1, seen)
    out = full(findings=(), evidence="df: 301 GiB free", population="volumes", predicate="floor", count=1)
    desk, chores, agent = build(
        tmp_path,
        khive,
        reader,
        [SWEEP, '<lact a1>answer(msg="m-q1", chore="disk")</lact>', DONE],
        disk=Probe("disk", out),
    )
    agent.state.put("desk_since", "2026-09-22T21:00:00Z", run="-", seq=0)  # go-live, before the row
    run = asyncio.run(agent.wake())
    assert run is not None and agent.wakes == 1
    # the watch waits the wake's whole poll from the go-live stamp, at no model cost
    assert (
        reader.ops[0] == 'comm.inbox(status="unread", limit=20, since="2026-09-22T21:00:00Z", wait_ms=5000)'
    )
    assert agent.state.get("desk_watch").value == "2026-09-22T21:59:17.849964Z"
    cursor = agent.state.get("cursor").value
    assert cursor["mail"] == [] and cursor["notices"] == ["watch:1"]  # the watch is not the agent's mail
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-q1", "answered")] and pending(agent) == {}
    calls, ops = len(agent.backend.calls), len(reader.ops)
    assert asyncio.run(agent.wake()) is None and agent.idle == 1 and len(agent.backend.calls) == calls
    assert reader.ops[ops:] == [  # the next poll reads from the saved cursor
        'comm.inbox(status="unread", limit=20, since="2026-09-22T21:59:17.849964Z", wait_ms=5000)'
    ]
    assert agent.state.get("desk_watch").value == "2026-09-22T21:59:17.849964Z"


def test_the_desks_own_mail_in_the_chairs_box_and_a_row_without_a_stamp_never_busy_loop(tmp_path):
    mine = inbox_row(SELF, "Re: x", "[desk] answered", "m-me", "t-x", at="2026-09-22T22:00:00.000001Z")
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, FakeReader(mine), [DONE])
    agent.state.put("desk_since", "2026-09-22T21:00:00Z", run="-", seq=0)
    assert asyncio.run(agent.wake()) is None and agent.state.get("desk_watch").value == (
        "2026-09-22T22:00:00.000002Z"
    )
    assert pending(agent) == {}
    bare = inbox_row(ASKER, "q", "no stamp on this row", "m-b", "t-b")
    desk, chores, agent = build(
        tmp_path / "b",
        khive,
        FakeReader(bare),
        [SWEEP, '<lact l1>leave(msg="m-b", why="notice")</lact>', DONE],
    )
    (tmp_path / "b").mkdir()
    assert asyncio.run(agent.wake()) is not None
    assert agent.state.get("desk_watch") is None  # a stampless row moves no cursor
    calls = len(agent.backend.calls)  # handled, it is read again each poll and wakes nothing
    assert asyncio.run(agent.wake()) is None and len(agent.backend.calls) == calls and pending(agent) == {}


def test_after_is_one_microsecond_past_the_stamp_as_since_reads_it():
    assert after("2026-09-22T21:59:17.849963Z") == "2026-09-22T21:59:17.849964Z"
    assert after("2026-09-22T21:59:17.999999+00:00") == "2026-09-22T21:59:18.000000Z"
    assert after("2026-09-22T17:59:17Z") == "2026-09-22T17:59:17.000001Z"


def test_a_failed_watch_is_an_error_line_and_the_desks_own_mail_still_runs(tmp_path):
    class Broken(FakeReader):
        async def exec(self, ops: str):
            if "wait_ms=" in ops:
                raise RuntimeError("daemon dropped the poll")
            return await super().exec(ops)

    khive = FakeKhive(mail(STEWARD, "tick: digest"))
    desk, chores, agent = build(tmp_path, khive, Broken(), ["<lact d1>digest()</lact>", DONE])
    run = asyncio.run(agent.wake())
    assert run is not None and agent.errors == ["watch failed: RuntimeError: daemon dropped the poll"]


def test_failed_selected_message_blocks_success_and_remains_pending(tmp_path):
    """An answer that fails (an unknown chore) takes nothing: the wake cannot end Success with the listed
    message unhandled, the message stays pending, and the next wake lists it again with no new arrival
    and handles it once."""
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1, NOTICE),
        [
            SWEEP,
            '<lact a1>answer(msg="m-q1", chore="nope")</lact><lact l1>leave(msg="m-n1", why="notice")</lact>',
            DONE,
            DONE,
            DONE,
            SWEEP,
            '<lact a2>answer(msg="m-q1", chore="disk")</lact>',
            DONE,
        ],
        disk=Probe("disk", full()),
    )
    run = asyncio.run(agent.wake())
    first = (
        type(run.outcome).__name__,
        [(r["msg"], r["class"]) for r in desk.rows()],
        sorted(pending(agent)),
    )
    assert first == ("Refused", [("m-n1", "left")], ["m-q1"])
    run = asyncio.run(agent.wake())  # no new mail in either box
    assert run is not None and type(run.outcome).__name__ == "Success"
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-n1", "left"), ("m-q1", "answered")]
    assert pending(agent) == {} and [s["to"] for s in khive.sent] == [ASKER]


def test_watch_scans_past_full_handled_page(tmp_path):
    """Twenty handled rows, newest, fill the first page; five older unhandled rows are behind them."""
    rows = [
        inbox_row(
            ASKER, f"q{i}", f"notice {i}", f"m-{i:02d}", f"t-{i:02d}", at=f"2026-09-22T10:00:{i:02d}.000000Z"
        )
        for i in range(25)
    ]
    khive = FakeKhive()
    old = "".join(f'<lact l{i}>leave(msg="m-{i:02d}", why="notice")</lact>' for i in range(5))
    desk, chores, agent = build(tmp_path, khive, FakeReader(*rows), [SWEEP, old, DONE])
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    desk.ledger.write_text(
        "".join(
            json.dumps(
                {"at": "2026-09-22 03:00:00", "msg": f"m-{i:02d}", "from": ASKER, "subject": f"q{i}"}
                | {"thread": f"t-{i:02d}", "class": "left", "run": "r0", "why": "notice"}
            )
            + "\n"
            for i in range(5, 25)
        )
    )
    run = asyncio.run(agent.wake())
    assert run is not None, "five unhandled rows sit behind a full page of handled ones and nothing wakes"
    assert sorted(r["msg"] for r in desk.rows()) == [f"m-{i:02d}" for i in range(25)]
    # every scan here paged: the cursor stays at go-live and the next watch reads the interval again
    assert pending(agent) == {} and agent.state.get("desk_watch") is None


def test_pending_rewakes_without_new_arrival(tmp_path):
    """The wake the watch caused ended without a sweep; nothing new arrives, and the message still wakes
    the desk."""
    q1 = inbox_row(ASKER, "disk?", "how much disk is free?", "m-q1", "t-q1", at="2026-09-22T21:59:17.849963Z")
    khive = FakeKhive()
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(q1),
        [DONE, SWEEP, '<lact a1>answer(msg="m-q1", chore="disk")</lact>', DONE],
        disk=Probe("disk", full()),
    )
    agent.state.put("desk_since", "2026-09-22T21:00:00Z", run="-", seq=0)
    assert asyncio.run(agent.wake()) is not None and desk.rows() == []
    run = asyncio.run(agent.wake())
    assert run is not None, "m-q1 is unhandled in the chair's box and no wake comes for it"
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-q1", "answered")] and pending(agent) == {}


def test_equal_timestamp_page_boundary_keeps_all_ids(tmp_path):
    """The three oldest rows share one stamp and the page boundary falls between them."""
    tie = "2026-09-22T10:00:00.500000Z"
    rows = [
        inbox_row(
            ASKER,
            f"q{i}",
            f"notice {i}",
            f"m-{i:02d}",
            f"t-{i:02d}",
            at=f"2026-09-22T10:00:{i + 1:02d}.000000Z",
        )
        for i in range(18)
    ] + [inbox_row(ASKER, f"q{i}", f"notice {i}", f"m-{i:02d}", f"t-{i:02d}", at=tie) for i in range(18, 21)]
    reader = FakeReader(*rows)
    khive = FakeKhive()
    desk, chores, agent = build(tmp_path, khive, reader, [])
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    asyncio.run(desk.watch(0))
    assert sorted(pending(agent)) == sorted(r["id"] for r in rows)
    assert [
        re.search(r"offset=(\d+)", op) and re.search(r"offset=(\d+)", op).group(1) for op in reader.ops
    ] == [
        None,
        "20",
    ]


def test_a_chair_message_whose_first_wake_failed_still_wakes_the_desk(tmp_path):
    """The own poll's answer is lost after the watch read the chair's row."""

    class OwnBoxDropsOnce(FakeKhive):
        failed = False

        async def inbox(self, **kw):
            if not self.failed:
                self.failed = True
                await asyncio.sleep(0.05)  # the watch's row landed first, then the own poll's answer was lost
                raise KhiveError(f"kkernel exited 1: {LOST}")
            return await super().inbox(**kw)

    q1 = inbox_row(ASKER, "disk?", "how much disk is free?", "m-q1", "t-q1", at="2026-09-22T21:59:17.849963Z")
    khive = OwnBoxDropsOnce()
    out = full(findings=(), evidence="df: 301 GiB free", population="volumes", predicate="floor", count=1)
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(q1),
        [SWEEP, '<lact a1>answer(msg="m-q1", chore="disk")</lact>', DONE],
        disk=Probe("disk", out),
    )
    agent.state.put("desk_since", "2026-09-22T21:00:00Z", run="-", seq=0)
    with pytest.raises(KhiveError):
        asyncio.run(agent.wake())  # serve logs this and polls again
    assert "m-q1" in pending(agent)  # discovery moved past the row only after it was pending
    run = asyncio.run(agent.wake())  # the next poll: the agent's box is healthy, the chair's row unhandled
    assert run is not None, "m-q1 is in the chair's box, unhandled, and no wake will ever come for it"
    assert [r["msg"] for r in desk.rows()] == ["m-q1"]


def test_every_unread_message_since_go_live_is_listed_by_some_sweep(tmp_path):
    """The desk handles every listed message; the chair reads none of them."""
    rows = [
        inbox_row(
            "lambda:y", f"q{i}", f"question {i}", f"s{i:02d}", f"t{i}", at=f"2026-09-22T10:00:{i:02d}.000000Z"
        )
        for i in range(25)
    ]
    desk, chores, agent = build(tmp_path, FakeKhive(), FakeReader(*rows), [])
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    run = types.SimpleNamespace(id="r1", record=types.SimpleNamespace(seq=0))
    listed: set[str] = set()
    for _ in range(3):
        out = asyncio.run(desk.sweep(run))
        ids = re.findall(r"\[msg (\S+) from", out)
        listed |= set(ids)
        for mid in ids:
            asyncio.run(desk.leave(Leave(msg=mid, why="judgment"), run))
    assert len(listed) == 25, f"never listed: {sorted({r['id'] for r in rows} - listed)}"


def test_a_burst_larger_than_the_page_is_listed_whole_across_sweeps(tmp_path):
    """A burst of 25 at a page of 20; the second sweep lists the rest."""
    rows = [
        inbox_row(
            ASKER, f"q{i}", f"notice {i}", f"m-{i:02d}", f"t-{i:02d}", at=f"2099-01-01T00:00:{i:02d}.000000Z"
        )
        for i in range(25)
    ]
    first = "".join(f'<lact l{i}>leave(msg="m-{i:02d}", why="notice")</lact>' for i in range(20))
    rest = "".join(f'<lact l{i}>leave(msg="m-{i:02d}", why="notice")</lact>' for i in range(20, 25))
    khive = FakeKhive(mail(SELF, "tick: sweep"))
    desk, chores, agent = build(tmp_path, khive, FakeReader(*rows), [SWEEP, first, DONE, SWEEP, rest, DONE])
    agent.state.put("desk_since", "2026-01-01T00:00:00Z", run="-", seq=0)
    run = asyncio.run(agent.wake())
    assert "5 more wait for the next sweep" in [r for r in results(run) if r.startswith("sweep:")][0]
    khive.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    run = asyncio.run(agent.wake())
    second = [r for r in results(run) if r.startswith("sweep:")][0]
    assert second.startswith(f"sweep: 5 new message(s) for {OWNER}")
    assert sorted(r["msg"] for r in desk.rows()) == sorted(r["id"] for r in rows)


def test_a_reply_refused_before_sending_stays_pending_and_a_later_attempt_holds_a_reply_of_its_own(tmp_path):
    """B2's control on the intake: a definite refusal spends that attempt only; the message stays pending,
    is listed again with no new arrival, and its next attempt sends on its thread."""
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    inner.refuse.add(ASKER)
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    answer = '<lact a1>answer(msg="m-q1", chore="disk")</lact>'
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1),
        [SWEEP, answer, DONE, DONE, DONE, SWEEP, answer.replace("a1", "a2"), DONE],
        disk=Probe("disk", full()),
    )
    run = asyncio.run(agent.wake())
    assert any("m-q1: reply not sent: hop limit" in r for r in results(run)) and desk.rows() == []
    assert list(pending(agent)) == ["m-q1"] and inner.sent == []
    inner.refuse.clear()
    asyncio.run(agent.wake())
    assert [(s["to"], s["thread_id"]) for s in inner.sent] == [(ASKER, "t-q1")]
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-q1", "answered")] and pending(agent) == {}


def test_a_failed_own_poll_joins_the_watch_before_the_wake_raises(tmp_path):
    """A watch still running after its wake raised would write intake during a later wake."""

    class SlowReader(FakeReader):
        finished = False

        async def exec(self, ops: str):
            await asyncio.sleep(0.05)
            out = await super().exec(ops)
            self.finished = True
            return out

    class OwnBoxFails(FakeKhive):
        async def inbox(self, **kw):
            raise KhiveError(f"kkernel exited 1: {LOST}")

    reader = SlowReader(Q1)
    desk, chores, agent = build(tmp_path, OwnBoxFails(), reader, [])

    async def wake() -> bool:
        with pytest.raises(KhiveError):
            await agent.wake()
        return reader.finished

    assert asyncio.run(wake()) is True


def test_watch_recovery_does_not_offer_mail_rerun(tmp_path):
    """A wake whose batch was the watch's notice alone dies mid-run; the next start names
    it as a notice and prescribes no rerun, which would refuse a watch id as not in the read box."""

    class Died:
        calls: list = []

        async def __call__(self, messages):
            raise SystemExit(1)  # the process died mid-run

    q1 = inbox_row(ASKER, "disk?", "how much disk is free?", "m-q1", "t-q1", at="2026-09-22T21:59:17.849963Z")
    khive = FakeKhive()  # the agent's own box is empty; only the chair's box has a row
    desk, chores, agent = build(tmp_path, khive, FakeReader(q1), [])
    agent.state.put("desk_since", "2026-09-22T21:00:00Z", run="-", seq=0)
    agent.owner, agent.backend = OWNER, Died()
    with pytest.raises(SystemExit):
        asyncio.run(agent.wake())
    assert agent.state.get("cursor").value["outcome"] == "running"
    asyncio.run(agent._recover())  # what serve does first on the restart
    (notice,) = khive.sent
    assert ("Agent.rerun" in notice["content"], "watch notices ['watch:1']" in notice["content"]) == (
        False,
        True,
    )
    assert "m-q1" in pending(agent)  # the chair's message is still owed an answer by the desk itself


class LostReceipt(FakeKhive):
    """The transport delivers the first send, then loses its answer: the desk cannot know it landed.
    While `down`, no send reaches the server at all."""

    lost = True
    down = False

    async def send(self, *args, **kwargs):
        if self.down:
            raise KhiveError("kkernel did not answer within 60s: comm.send")
        sent = await super().send(*args, **kwargs)
        if self.lost:
            self.lost = False
            raise KhiveError(f"kkernel exited 1: {LOST}")
        return sent


def test_a_delivery_unresolved_is_refused_a_second_send_and_its_keyed_replay_settles_it(tmp_path):
    """A send whose answer was lost is a delivery unresolved. While its replay fails the sweep lists it
    on its own line, `answer` is refused with that line and nothing is written; the replay that the
    server answers `replayed` settles it with the message that landed, and no thread is read."""
    inner = LostReceipt()
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    q = inbox_row(ASKER, "disk?", "how much disk?", "m-q1", "t-q1", at="2026-09-22T10:00:00.000000Z")
    reader = FakeReader(q)
    answer = '<lact a1>answer(msg="m-q1", chore="disk")</lact>'
    desk, chores, agent = build(tmp_path, khive, reader, [SWEEP, answer, DONE], disk=Probe("disk", full()))
    agent.state.put("desk_since", "2026-09-22T09:00:00Z", run="-", seq=0)
    run = asyncio.run(agent.wake())
    assert type(run.outcome).__name__ == "Success" and len(inner.sent) == 1 and desk.rows() == []
    send = pending(agent)["m-q1"]["send"]
    assert (send["to"], send["thread"], send["subject"], send["attempt"], send["key"]) == (
        ASKER,
        "t-q1",
        "Re: disk?",
        run.id,
        f"desk:{SELF}:m-q1:answered",
    )
    assert send["content"] == inner.sent[0]["content"]  # the exact request a replay sends again
    line = f"m-q1: delivery unresolved since {send['started']}, to {ASKER} on t-q1: leave(...) it or wait"
    inner.down = True
    inner.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    agent.backend.turns = [SWEEP, '<lact a2>answer(msg="m-q1", chore="disk")</lact>', DONE]
    run = asyncio.run(agent.wake())  # every replay fails: nothing settles it, nothing new is sent
    assert type(run.outcome).__name__ == "Success" and sum(line in r for r in results(run)) == 2
    assert len(inner.sent) == 1 and desk.rows() == [] and pending(agent)["m-q1"]["send"] == send
    assert agent.errors[-1].startswith("desk: replay of m-q1 unresolved: KhiveError: kkernel did not answer")
    inner.down = False
    assert asyncio.run(agent.wake()) is None  # the watch settles it; nothing is left to wake for
    rows = [(r["class"], r.get("reply"), r.get("chore"), r["run"], r["settled"]) for r in desk.rows()]
    assert rows == [("answered", "m1", "disk", send["attempt"], "replayed")]
    assert pending(agent) == {} and len(inner.sent) == 1
    assert not any(op.startswith("comm.thread(") for op in reader.ops) and len(reader.ops) > 0


def test_leaving_a_delivery_unresolved_writes_its_row_and_sends_nothing(tmp_path):
    """The model's disposition for a delivery unresolved is `leave`; its row says why whatever the model
    named, and carries the attempt."""
    inner = LostReceipt(mail(SELF, "tick: sweep"))
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(Q1),
        [SWEEP, '<lact a1>answer(msg="m-q1", chore="disk")</lact>', DONE],
        disk=Probe("disk", full()),
    )
    first = asyncio.run(agent.wake())
    inner.down = True  # the replay cannot settle it, so the sweep lists it for a disposition
    inner.mail.append(mail(SELF, "tick: sweep", mid="b2c3d4e5"))
    agent.backend.turns = [SWEEP, '<lact l1>leave(msg="m-q1", why="notice")</lact>', DONE]
    asyncio.run(agent.wake())
    rows = [(r["msg"], r["class"], r["why"], (r.get("send") or {}).get("attempt")) for r in desk.rows()]
    assert rows == [("m-q1", "left", "delivery unresolved", first.id)]
    assert pending(agent) == {} and len(inner.sent) == 1


def test_a_clarify_refused_before_sending_stays_answerable_and_is_sent_on_the_next_wake(tmp_path):
    """B2's control on the clarify path: a definite refusal before the send leaves no delivery record, so
    the message is listed again with no new arrival and its next attempt sends on its thread."""
    inner = FakeKhive(mail(SELF, "tick: sweep"))
    inner.refuse.add(ASKER)
    khive = Bounded(inner, recipients=frozenset({OWNER, STEWARD, SELF}))
    readings = 'msg="m-c1", a="the disk under the roots", b="PR 12 that is red"'
    desk, chores, agent = build(
        tmp_path,
        khive,
        FakeReader(CQ),
        [
            SWEEP,
            f"<lact c1>clarify({readings})</lact>",
            DONE,
            DONE,
            DONE,
            SWEEP,
            f"<lact c2>clarify({readings})</lact>",
            DONE,
        ],
    )
    run = asyncio.run(agent.wake())
    assert any("m-c1: clarify not sent: hop limit" in r for r in results(run)) and desk.rows() == []
    assert "send" not in pending(agent)["m-c1"] and inner.sent == []
    inner.refuse.clear()
    assert asyncio.run(agent.wake()) is not None  # no new arrival: the pending message wakes the desk
    assert [(s["to"], s["thread_id"]) for s in inner.sent] == [(ASKER, "t-c1")]
    assert [(r["msg"], r["class"]) for r in desk.rows()] == [("m-c1", "clarified")] and pending(agent) == {}


def test_the_reader_reaches_kkernel_through_its_admitted_read_verbs_only(tmp_path, monkeypatch):
    """The reader reaches kkernel as the chair through its admitted `exec` only: no helper of the chair's
    client marks, sends or is granted a recipient. A helper that is not there is a refusal too."""
    chair = tmp_path / "chair"
    (chair / ".khive").mkdir(parents=True)
    (chair / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    stub = tmp_path / "kkernel"
    stub.write_text("")
    reached: list[tuple[str, str]] = []

    async def recorded(self, ops):
        reached.append((self.actor, ops))
        return [{"messages": []}]

    monkeypatch.setattr(Khive, "exec", recorded)
    reader = Reader(Khive(chair, "lambda:x", kkernel=str(stub)))
    assert reader.allowed == READ_VERBS and reader.actor == "lambda:x" and reader.cwd == chair
    assert not any(hasattr(reader, n) for n in ("send", "grant", "mark_read", "inbox"))
    for name, args in (
        ("mark_read", (["m-q1"],)),
        ("grant", ("lambda:a",)),
        ("send", ("lambda:a", "as the chair")),
        ("inbox", ()),
    ):
        try:
            out = getattr(reader, name)(*args)
            if asyncio.iscoroutine(out):
                asyncio.run(out)
        except (AttributeError, AgentError):
            pass
    refused = []
    for ops in (
        'comm.read(ids=["m-q1"])',
        '{"tool": "comm.read", "args": {"ids": ["m-q1"]}}',
        '{"tool": "comm.send", "args": {"to": "email:someone@example.com", "content": "hi"}}',
        '[{"tool": "gtd.complete", "args": {"id": "00000000-0000-0000-0000-000000000000"}}]',
        '{"tool": "memory.remember", "args": {"content": "x", "salience": 0.99}}',
        '{"tool": "delete", "args": {"id": "x"}}',
    ):
        try:
            asyncio.run(reader.exec(ops))
        except AgentError as e:
            refused.append(str(e).startswith("agent boundary"))
    assert reached == [], f"kkernel was handed {reached} through the reader"
    assert refused == [True] * 6
    asyncio.run(reader.exec('comm.inbox(status="unread", limit=1)'))  # the recorder is live
    assert reached == [("lambda:x", 'comm.inbox(status="unread", limit=1)')]
