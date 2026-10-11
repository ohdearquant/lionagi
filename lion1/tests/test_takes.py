"""A take of a CLI session (ADR-0025 D12, S21): it begins with `lion.take_begin`, compares each held
prompt's hash with the transcript's, and appends to the tip, branches where the prompts part, or opens
the root; every harness write carries the take's generation; a restore reads the tip's held view through
its branches. On the store's double, in this process and in two real processes, and on the built binary
when `LION1_KHIVE` names it."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from lion_cli.context import checkpoint, taken

from hub.backend.checkpoint import (
    exchanges_of,
    held_session,
    held_view,
    record_exchanges,
    session_of,
)
from lion1.checkpoint import row_json
from lion1.khive import KhiveError, lit
from lion1.store import append_messages
from tests.lionfake import LionFake, LionKhive, routed_config
from tests.test_carried import BIG, NOWHERE, Recording, Sent, carried_append, count
from tests.test_checkpoint import turn
from tests.test_context import AT_TAKE, FIXTURE, TASK, TaskKhive, actor_dir, gh_says
from tests.test_store import ACTOR, _stop, binary_dirs  # noqa: F401 (a fixture)

__all__ = ()

REPO = Path(__file__).resolve().parent.parent
TREE = (".", "src", "apps/cli")  # the three packages a child of this tree imports from it
KW = {"source": "claude-code", "actor": ACTOR, "transcript": "/t/cc.jsonl", "why": "manual"}
OLDER_ROW = "this row carries none \\(a client older than takes\\)"
OLDER_OPEN = "this open carries none \\(a client older than takes\\)"

# a take in a process of its own, over the stub `kkernel`: with a pause file, its first `lion.take_begin`
# stands, its generation in hand, until the go file is there
CHILD = """
import asyncio, json, sys, time
from pathlib import Path

from hub.backend.checkpoint import record_exchanges
from lion1.khive import Khive

d, actor, sid, scan, paused, go = sys.argv[1:]


class Paused(Khive):
    begun = 0

    async def exec(self, ops):
        got = await super().exec(ops)
        if "lion.take_begin" in ops:
            self.begun += 1
            if self.begun == 1 and paused:
                Path(paused).touch()
                while not Path(go).exists():
                    time.sleep(0.02)
        return got


k = Paused(Path(d), actor)
kw = {"source": "claude-code", "actor": actor, "transcript": "/t/cc.jsonl", "why": "manual"}
took = asyncio.run(record_exchanges(k, session_id=sid, exchanges=json.loads(scan), **kw))
print(json.dumps({"took": took, "begun": k.begun}))
"""


class Outrun:
    """A store over which another take of the same CLI session runs whole just before this take's first
    call holding `verb` is sent: the generation this take holds is superseded by then."""

    def __init__(self, k, sid: str, other: list[dict], verb: str) -> None:
        self.k, self.sid, self.other, self.verb, self.ran = k, sid, other, verb, False
        self.sent: list[str] = []

    async def exec(self, ops: str) -> list:
        if self.verb in ops and not self.ran:
            self.ran = True
            await record_exchanges(self.k, session_id=self.sid, exchanges=self.other, **KW)
        self.sent.append(ops)
        return await self.k.exec(ops)


def changed(i: int) -> dict:
    return {**turn(i), "input": f"prompt {i}, rewritten"}


def take(k, exchanges: list[dict], sid: str) -> tuple[str, int, int]:
    return asyncio.run(record_exchanges(k, session_id=sid, exchanges=exchanges, **KW))


def begin(k, sid: str) -> dict:
    (got,) = asyncio.run(k.exec(f"lion.take_begin(provider_session_id={lit(sid)})"))
    return got


def tip_of(k, sid: str) -> str:
    (got,) = asyncio.run(k.exec(f'lion.get(table="takes", id={lit(sid)})'))
    return got["tip"]


def inputs(k, sid: str) -> list[str]:
    return [e["input"] for e in asyncio.run(exchanges_of(k, sid))]


def landed(k, sid: str, seq: int, exchange: dict, generation: int) -> None:
    """The exchange's row as a take writes it under `generation`, as one whose earlier row failed
    leaves it."""
    raw = row_json({**exchange, "why": "manual"}, ensure_ascii=False)
    row = {"seq": seq, "role": "user", "kind": "exchange", "text": exchange["input"], "raw": raw, "turn": seq}
    asyncio.run(append_messages(k, sid, [{**row, "generation": generation}]))


def snapshot(k, sid: str) -> str:
    """The session's row and its message rows, as the store holds them."""
    from lion1.store import read_messages

    return json.dumps([asyncio.run(session_of(k, sid)), asyncio.run(read_messages(k, sid))], sort_keys=True)


def open_branch(sid: str, of: str, seq: int, generation: int, id: str) -> str:
    return (
        f'lion.session_open(id={lit(id)}, source="claude_code", provider_session_id={lit(sid)}, '
        f'actor={lit(ACTOR)}, transcript="/t/cc.jsonl", branch_of={lit(of)}, branch_seq={seq}, '
        f"generation={generation})"
    )


# --- the cases, each on any store: the double here, the built binary below ---


def an_older_clients_harness_writes_are_refused_by_name(k) -> None:
    # S21: a client that predates takes sends a harness row or open with no generation; each is refused
    # by name and nothing is written
    assert take(k, [turn(0)], "cc-old") == ("cc-old", 1, 1)
    held = snapshot(k, "cc-old")
    row = 'lion.message_append(session_id="cc-old", seq=1, role="user", kind="exchange", text="late", turn=1)'
    for ops, refusal in (
        (row, f"session cc-old is a claude_code session, written by a take .* {OLDER_ROW}"),
        (carried_append("cc-old", 1, {"size": 5, "pieces": [NOWHERE]}), OLDER_ROW),
        (
            f'lion.session_open(source="claude_code", provider_session_id="cc-old2", actor={lit(ACTOR)})',
            f"a claude_code session is opened by a take, .* {OLDER_OPEN}",
        ),
    ):
        with pytest.raises(KhiveError, match=refusal):
            asyncio.run(k.exec(ops))
    assert snapshot(k, "cc-old") == held
    assert asyncio.run(session_of(k, "cc-old2")) is None


def a_take_field_where_no_take_writes_is_refused_by_name(k) -> None:
    asyncio.run(k.exec(f'lion.session_open(id="run-g", source="lion1", actor={lit(ACTOR)})'))
    for ops, refusal in (
        (
            'lion.message_append(session_id="run-g", seq=0, role="user", kind="in", text="t", generation=1)',
            "session run-g is a lion1 session, which no take writes",
        ),
        ('lion.session_open(source="lion1", generation=1)', "a lion1 session is no take's"),
        ('lion.take_begin(provider_session_id="")', "a take names its provider_session_id"),
        ('lion.session_open(source="codex", generation=1)', "a codex session names its provider_session_id"),
        (
            'lion.session_open(source="codex", provider_session_id="c-x", generation=1, branch_of="x")',
            "a branch names its branch_of and its branch_seq",
        ),
        ('lion.get(table="takes", id="c-never")', "takes c-never"),
        (
            'lion.session_open(source="codex", provider_session_id="c-none", generation=1)',
            "take 1 of c-none is superseded: no take of it has begun",
        ),
    ):
        with pytest.raises(KhiveError, match=refusal):
            asyncio.run(k.exec(ops))
    assert asyncio.run(session_of(k, "run-g"))["message_count"] == 0


def an_agreeing_take_fills_holes_in_place_and_keeps_a_longer_held_tail(k) -> None:
    p = [turn(i) for i in range(4)]
    assert take(k, p[:1], "cc-agree") == ("cc-agree", 1, 1)
    landed(k, "cc-agree", 2, p[2], begin(k, "cc-agree")["generation"])  # seq 1 failed, seq 2 stood
    assert take(k, p, "cc-agree") == ("cc-agree", 4, 2)  # seq 1 in its hole, seq 3 past the end
    assert inputs(k, "cc-agree") == [e["input"] for e in p]
    assert take(k, p[:2], "cc-agree") == ("cc-agree", 4, 0)  # a shorter transcript: the tail stays
    assert inputs(k, "cc-agree") == [e["input"] for e in p] and tip_of(k, "cc-agree") == "cc-agree"


def a_divergence_with_no_hole_branches_at_d_and_leaves_the_parent_byte_identical(k) -> None:
    p, q = [turn(i) for i in range(3)], [turn(0), turn(1), changed(2), changed(3)]
    assert take(k, p, "cc-div") == ("cc-div", 3, 3)
    parent = snapshot(k, "cc-div")
    tip, turns, appended = take(k, q, "cc-div")
    assert tip != "cc-div" and (turns, appended) == (4, 2)
    s = asyncio.run(session_of(k, tip))
    assert (s["branch_of"], s["branch_seq"]) == ("cc-div", 2)
    assert (s["source"], s["provider_session_id"], s["actor"], s["transcript"]) == (
        "claude_code",
        "cc-div",
        ACTOR,
        "/t/cc.jsonl",
    )
    assert snapshot(k, "cc-div") == parent
    assert inputs(k, tip) == [e["input"] for e in q]
    assert [r["session_id"] for r in asyncio.run(held_view(k, tip))] == ["cc-div", "cc-div", tip, tip]
    # the CLI session's id names its tip; a branch's own id names that branch
    assert tip_of(k, "cc-div") == tip == asyncio.run(held_session(k, ACTOR, "cc-div"))["id"]
    assert asyncio.run(held_session(k, ACTOR, tip))["id"] == tip
    assert take(k, q, "cc-div") == (tip, 4, 0)  # the same transcript again: no branch, nothing written


def a_hole_below_the_divergence_moves_the_branch_to_the_hole(k) -> None:
    p = [turn(i) for i in range(4)]
    assert take(k, p[:1], "cc-hole") == ("cc-hole", 1, 1)
    g = begin(k, "cc-hole")["generation"]
    landed(k, "cc-hole", 2, p[2], g)
    landed(k, "cc-hole", 3, p[3], g)
    parent = snapshot(k, "cc-hole")
    scan = [*p[:3], changed(3)]  # parts at 3; the view lacks 1
    tip, turns, appended = take(k, scan, "cc-hole")
    assert (turns, appended) == (4, 3)
    assert asyncio.run(session_of(k, tip))["branch_seq"] == 1
    assert inputs(k, tip) == [e["input"] for e in scan]
    assert snapshot(k, "cc-hole") == parent


def a_first_take_adopts_no_session_of_another_source_or_cli_session(k) -> None:
    # a session of this runtime holds the id
    asyncio.run(k.exec(f'lion.session_open(id="cc-run", source="lion1", actor={lit(ACTOR)})'))
    tip, _, _ = take(k, [turn(0)], "cc-run")
    assert tip != "cc-run" and asyncio.run(session_of(k, tip))["provider_session_id"] == "cc-run"
    assert asyncio.run(session_of(k, "cc-run"))["source"] == "lion1"
    # a harness session of another CLI session holds it
    g = begin(k, "cc-q")["generation"]
    asyncio.run(
        k.exec(
            f'lion.session_open(id="cc-p", source="claude_code", provider_session_id="cc-q", '
            f"actor={lit(ACTOR)}, generation={g})"
        )
    )
    tip, _, _ = take(k, [turn(0)], "cc-p")
    assert tip not in ("cc-p", "cc-q") and tip_of(k, "cc-p") == tip and tip_of(k, "cc-q") == "cc-p"
    assert asyncio.run(session_of(k, "cc-p"))["provider_session_id"] == "cc-q"


def a_superseded_take_begins_again_and_lands(k) -> None:
    # outrun at its open: the root another take opened stands, and this take branches from it
    mine, other = [turn(0), turn(1), changed(2)], [turn(0), turn(1), turn(2)]
    out = Outrun(k, "cc-sup", other, "lion.session_open")
    tip, turns, appended = take(out, mine, "cc-sup")
    assert count(out.sent, "lion.take_begin") == 2 and (turns, appended) == (3, 1)
    assert inputs(k, tip) == [e["input"] for e in mine] and tip_of(k, "cc-sup") == tip
    assert inputs(k, "cc-sup") == [e["input"] for e in other]
    # outrun at its appends: its rows are refused, and the take compares again with what landed
    assert take(k, [turn(0)], "cc-sup2") == ("cc-sup2", 1, 1)
    mine, other = [turn(0), turn(1), turn(2)], [turn(0), changed(1)]
    out = Outrun(k, "cc-sup2", other, "lion.message_append")
    tip, turns, appended = take(out, mine, "cc-sup2")
    assert count(out.sent, "lion.take_begin") == 2 and (turns, appended) == (3, 2)
    assert asyncio.run(session_of(k, tip))["branch_seq"] == 1
    assert inputs(k, tip) == [e["input"] for e in mine] and tip_of(k, "cc-sup2") == tip
    assert inputs(k, "cc-sup2") == [e["input"] for e in other]


def begins_never_share_a_generation(k) -> None:
    ops = ", ".join(['lion.take_begin(provider_session_id="cc-begin")'] * 8)
    got = [r["generation"] for _ in range(3) for r in asyncio.run(k.exec(f"[{ops}]"))]
    assert sorted(got) == list(range(1, 25))
    last = begin(k, "cc-begin")
    assert (last["provider_session_id"], last["generation"], last["tip"]) == ("cc-begin", 25, None)


def a_branch_open_whose_tip_move_fails_leaves_no_session_row(k) -> None:
    assert take(k, [turn(0)], "cc-move") == ("cc-move", 1, 1)
    g = begin(k, "cc-move")["generation"]
    (first,) = asyncio.run(k.exec(open_branch("cc-move", "cc-move", 0, g, "cc-move-1")))
    assert first["id"] == "cc-move-1" and tip_of(k, "cc-move") == "cc-move-1"
    # the same take opening from the tip it read: another stands there now
    with pytest.raises(
        KhiveError, match="the tip of cc-move is cc-move-1, not cc-move: the take read another, and nothing"
    ):
        asyncio.run(k.exec(open_branch("cc-move", "cc-move", 0, g, "cc-move-2")))
    # an older take opening from the tip: a newer take has begun
    begin(k, "cc-move")
    with pytest.raises(KhiveError, match=f"take {g} of cc-move is superseded: the current take is {g + 1}"):
        asyncio.run(k.exec(open_branch("cc-move", "cc-move-1", 0, g, "cc-move-3")))
    for gone in ("cc-move-2", "cc-move-3"):
        assert asyncio.run(session_of(k, gone)) is None
    assert tip_of(k, "cc-move") == "cc-move-1"
    # the move made and the session row refused, its id another session's: the move rolls back with it
    g = begin(k, "cc-move")["generation"]
    with pytest.raises(
        KhiveError, match=f"session cc-move was not opened: take {g} of cc-move is not current, or another"
    ):
        asyncio.run(k.exec(open_branch("cc-move", "cc-move-1", 0, g, "cc-move")))
    assert tip_of(k, "cc-move") == "cc-move-1" and asyncio.run(session_of(k, "cc-move"))["branch_of"] is None


def a_three_deep_chain_restores_as_its_composed_view(k) -> None:
    root = [turn(i) for i in range(4)]
    one = [turn(0), turn(1), changed(2), changed(3)]
    two = [turn(0), turn(1), changed(2), {**turn(3), "input": "prompt 3, again"}, turn(4)]
    assert take(k, root, "cc-deep") == ("cc-deep", 4, 4)
    b1, _, _ = take(k, one, "cc-deep")
    b2, turns, appended = take(k, two, "cc-deep")
    assert (turns, appended) == (5, 2)
    assert asyncio.run(session_of(k, b2))["branch_of"] == b1
    assert asyncio.run(held_session(k, ACTOR, "cc-deep"))["id"] == b2
    assert inputs(k, b2) == [e["input"] for e in two]
    assert [r["session_id"] for r in asyncio.run(held_view(k, b2))] == ["cc-deep", "cc-deep", b1, b2, b2]
    assert inputs(k, b1) == [e["input"] for e in one] and inputs(k, "cc-deep") == [e["input"] for e in root]


def the_walk_fetches_pieces_for_the_rows_kept_alone(k) -> None:
    big = [turn(0), turn(1), {**turn(2), "input": BIG}]
    assert take(k, big, "cc-big") == ("cc-big", 3, 3)
    tip, _, _ = take(k, [turn(0), changed(1)], "cc-big")
    before = count(k.sent, "blob.get")
    assert inputs(k, tip) == ["prompt 0", "prompt 1, rewritten"]
    assert count(k.sent, "blob.get") == before  # the parent's carried row lies past the cutoff
    assert inputs(k, "cc-big")[2] == BIG and count(k.sent, "blob.get") > before


CASES = (
    an_older_clients_harness_writes_are_refused_by_name,
    a_take_field_where_no_take_writes_is_refused_by_name,
    an_agreeing_take_fills_holes_in_place_and_keeps_a_longer_held_tail,
    a_divergence_with_no_hole_branches_at_d_and_leaves_the_parent_byte_identical,
    a_hole_below_the_divergence_moves_the_branch_to_the_hole,
    a_first_take_adopts_no_session_of_another_source_or_cli_session,
    a_superseded_take_begins_again_and_lands,
    begins_never_share_a_generation,
    a_branch_open_whose_tip_move_fails_leaves_no_session_row,
    a_three_deep_chain_restores_as_its_composed_view,
    the_walk_fetches_pieces_for_the_rows_kept_alone,
)


@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_a_take_on_the_double(case):
    case(Sent())


def test_an_older_clients_append_without_a_generation_is_refused_by_name():
    # S21, named for the record: the row of a harness session, inline or carried, and its open
    an_older_clients_harness_writes_are_refused_by_name(Sent())


def test_a_take_superseded_at_every_try_is_refused_by_name():
    # a newer take begins just before every append of this one: it begins again SUPERSEDED times, then
    # is refused by name rather than looping, and the store holds what the newer takes left
    from hub.backend import checkpoint as module

    k = Sent()

    class Outrun:
        begins = 0

        async def exec(self, ops: str) -> list:
            if "lion.take_begin" in ops:
                self.begins += 1
            if "lion.message_append" in ops:
                await record_exchanges(k, session_id="cc-cap", exchanges=[turn(0)], **KW)
            return await k.exec(ops)

    out = Outrun()
    with pytest.raises(KhiveError, match="the take of cc-cap was superseded 4 times in a row by newer takes"):
        asyncio.run(record_exchanges(out, session_id="cc-cap", exchanges=[turn(0), turn(1)], **KW))
    assert out.begins == module.SUPERSEDED + 1 == 4
    assert inputs(k, "cc-cap") == [turn(0)["input"]] and tip_of(k, "cc-cap") == "cc-cap"


def test_the_restore_walk_refuses_a_loop_and_a_missing_parent_by_name():
    # D12 row 4: the store's verbs never make such a chain, so the double's rows are set by hand, as a
    # store damaged or written by another client would hold them
    k = Sent()
    assert take(k, [turn(0), turn(1)], "cc-walk") == ("cc-walk", 2, 2)
    b, _, _ = take(k, [turn(0), changed(1)], "cc-walk")
    rows = k.lion.sessions
    rows[b]["branch_of"] = "cc-gone"
    for read in (held_view, exchanges_of):
        with pytest.raises(
            KhiveError, match=f"session {b} branches from cc-gone, which the store does not hold"
        ):
            asyncio.run(read(k, b))
    rows[b]["branch_of"] = "cc-walk"
    rows["cc-walk"]["branch_of"], rows["cc-walk"]["branch_seq"] = b, 1
    with pytest.raises(KhiveError, match=f"session {b} branches from itself through {b} <- cc-walk") as e:
        asyncio.run(exchanges_of(k, b))
    assert e.value.kind == "invalid"
    rows["cc-walk"]["branch_of"] = "cc-walk"  # a session that continues itself
    with pytest.raises(KhiveError, match="session cc-walk branches from itself through cc-walk$"):
        asyncio.run(held_view(k, "cc-walk"))
    with pytest.raises(KhiveError, match="session nosuch, which the store does not hold") as e:
        asyncio.run(held_view(k, "nosuch"))
    assert e.value.kind == "not_found"
    k.lion.takes["cc-walk"]["tip"] = "cc-lost"
    with pytest.raises(
        KhiveError, match="the takes of cc-walk name the tip cc-lost, which the store does not"
    ):
        asyncio.run(held_session(k, ACTOR, "cc-walk"))


def test_a_first_take_adopts_a_session_of_its_own_cli_session_from_before_takes():
    # S21: a store written before takes holds the session under the provider session id and no takes
    # row; the first take adopts it and appends past it. The binary's verbs cannot make that store.
    k = Sent()
    assert take(k, [turn(0)], "cc-adopt") == ("cc-adopt", 1, 1)
    del k.lion.takes["cc-adopt"]
    held = snapshot(k, "cc-adopt")
    assert take(k, [turn(0), turn(1)], "cc-adopt") == ("cc-adopt", 2, 1)
    assert tip_of(k, "cc-adopt") == "cc-adopt" and inputs(k, "cc-adopt") == ["prompt 0", "prompt 1"]
    assert json.loads(snapshot(k, "cc-adopt"))[0] == json.loads(held)[0]  # adopted, not opened again
    assert asyncio.run(held_session(k, ACTOR, "cc-none")) is None  # no takes row, no session


def test_the_walk_reads_a_parent_to_its_cutoff_and_no_page_past_it():
    k = Sent()
    long = [turn(i) for i in range(450)]
    assert take(k, long, "cc-long") == ("cc-long", 450, 450)
    scan = [*long[:300], *(changed(i) for i in range(300, 450))]
    tip, _, _ = take(k, scan, "cc-long")
    before = count(k.sent, "lion.messages")
    view = asyncio.run(held_view(k, tip))
    # the parent's two pages to seq 299, and the branch's one: never the parent's third
    assert count(k.sent, "lion.messages") - before == 3
    assert [r["seq"] for r in view] == list(range(450)) and view[300]["session_id"] == tip
    assert [r["text"] for r in view] == [e["input"] for e in scan]


def test_two_overlapping_takes_in_two_processes_end_with_one_tip_and_the_last_finishers_content(
    tmp_path, kkernel_stub
):
    # each take is a process of its own, every call a stub `kkernel` process over one store. The first
    # take begins and stands; the second runs whole; the first then writes under a superseded
    # generation, begins again, compares with what the second left, and finishes last
    d = tmp_path / "actor"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config(ACTOR, d))
    first, second = [turn(0), turn(1), changed(2), changed(3)], [turn(0), turn(1), turn(2)]
    paused, go = tmp_path / "paused", tmp_path / "go"

    def run(scan: list[dict], pause: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", CHILD, str(d), ACTOR, "cc-two", json.dumps(scan), pause, str(go)],
            cwd=REPO,
            # this tree's three packages, so the child runs the code under test, not an installed copy
            env={**os.environ, "PYTHONPATH": os.pathsep.join(str(REPO / p) for p in TREE)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    a = run(first, str(paused))
    deadline = time.monotonic() + 60
    while not paused.exists():
        assert a.poll() is None, a.communicate()
        assert time.monotonic() < deadline, "the first take never began"
        time.sleep(0.05)
    out, err = run(second, "").communicate(timeout=120)
    assert err == "", err
    b = json.loads(out)
    go.touch()
    out, err = a.communicate(timeout=120)
    assert a.returncode == 0 and err == "", err
    a_took = json.loads(out)
    assert (b["begun"], b["took"]) == (1, ["cc-two", 3, 3])
    assert a_took["begun"] == 2 and a_took["took"][1:] == [4, 2]
    k = LionKhive(ACTOR)
    k.lion = LionFake.from_json(json.loads(kkernel_stub.with_name("lion.json").read_text())[ACTOR])
    tip = a_took["took"][0]
    assert k.lion.takes["cc-two"]["tip"] == tip
    harness = [s for s in k.lion.sessions.values() if s["provider_session_id"] == "cc-two"]
    continued = {s["branch_of"] for s in harness}
    assert [s["id"] for s in harness if s["id"] not in continued] == [tip]  # one session nothing continues
    assert inputs(k, tip) == [e["input"] for e in first]
    assert inputs(k, "cc-two") == [e["input"] for e in second]


def test_lion_context_takes_a_rewritten_transcript_as_a_branch_and_restores_its_view(tmp_path, monkeypatch):
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    k = TaskKhive([TASK])
    live = tmp_path / "live.jsonl"
    live.write_text(FIXTURE.read_text())
    first = checkpoint(d, "manual", transcript=live, khive=k, now=AT_TAKE)
    assert (first["id"], first["session"], first["appended"]) == ("live@14", "live", 14)
    live.write_text(FIXTURE.read_text().replace("placeholder prompt 10", "placeholder prompt ten, rewritten"))
    branch = checkpoint(d, "rotate", transcript=live, khive=k, now=AT_TAKE)
    tip = branch["session"]
    assert tip != "live" and (branch["id"], branch["turns"], branch["appended"]) == (f"{tip}@14", 14, 2)
    assert k.lion.sessions[tip]["branch_of"] == "live"
    for named in (None, "live", tip):
        ckpt = taken(d, named, khive=k, now=AT_TAKE)
        assert ckpt.id == f"{tip}@14"
        assert "placeholder prompt ten, rewritten" in json.dumps(ckpt.view)
        assert ckpt.source["session_id"] == tip and ckpt.source["provider_session_id"] == "live"
    assert "rewritten" not in json.dumps(taken(d, f"{tip}@12", khive=k, now=AT_TAKE).view)


def built(binary_dirs, monkeypatch) -> tuple[str, Path, Recording]:  # noqa: F811
    """The client over the built binary, in the test's own short directories."""
    binary = os.environ["LION1_KHIVE"]
    monkeypatch.delenv("KHIVE_DB", raising=False)
    monkeypatch.delenv("KHIVE_NO_DAEMON")
    home, d = binary_dirs
    monkeypatch.setenv("HOME", str(home))
    (d / ".khive").mkdir()
    (d / ".khive" / "config.toml").write_text(routed_config(ACTOR, d, packs=("kg", "lion", "blob")))
    return binary, d, Recording(d, ACTOR, kkernel=binary)


@pytest.mark.skipif(not os.environ.get("LION1_KHIVE"), reason="LION1_KHIVE names no built binary")
@pytest.mark.launches_cli("lion1-khive")
@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_a_take_against_the_built_binary(case, binary_dirs, monkeypatch):  # noqa: F811
    binary, d, k = built(binary_dirs, monkeypatch)
    case(k)
    _stop(binary, d)
