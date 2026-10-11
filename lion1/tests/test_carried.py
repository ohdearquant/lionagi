"""A message past one argument (ADR-0025 C7, D8 to D11): a `text`, `raw` or `directive` whose literal
passes `INLINE_BYTES` goes to the blob store as pieces the row names, every call stays within one
argument, a session is read a page at a time, and a write that failed part way is finished by seq. On the
store's double, and on the built binary when `LION1_KHIVE` names it."""

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import dataclass, field, replace

import pytest

from hub.backend.checkpoint import exchanges_of, record_exchanges
from lion1 import Actor, Profile, Reply, RunRequest, Store
from lion1.checkpoint import row_json
from lion1.khive import ARGUMENT_BYTES, LOST, Khive, KhiveError, lit
from lion1.store import (
    INLINE_BYTES,
    PIECE_BYTES,
    PIECES_PER_READ,
    REFS_INLINE,
    append_messages,
    read_messages,
)
from tests.lionfake import LionKhive, routed_config
from tests.test_actor import Done, scripted
from tests.test_checkpoint import turn
from tests.test_store import ACTOR, TURNS, _stop, binary_dirs  # noqa: F401 (a fixture)

__all__ = ()

# 308,000 bytes, past 300 KiB, that the literal and the bytes disagree on: ASCII, a character JSON escapes
# to six bytes, and one past the basic plane that it escapes to twelve
BIG = "plain text, " * 9000 + "雨" * 60000 + "🙂" * 5000
BIG_EXCHANGES = [turn(0), {**turn(1, BIG), "input": BIG}, turn(2)]
NOWHERE = "0" * 64  # a ref no piece has
# 3 MiB of counting, so no two pieces are alike: one text repeated would be one piece, its ref shared
HUGE = [{**turn(0), "input": "".join(f"{i:07d}," for i in range(3 * 1024 * 1024 // 8))}]
CUT = "a" + "🙂" * 30_000  # the join after the first piece falls inside a four-byte character
INDEXED = "x" * (REFS_INLINE * PIECE_BYTES + 1)  # one piece past the listed refs: an index
SMALL_INPUTS = tuple(f"input {i} " + "s" * 2000 for i in range(120))  # 100 such rows pass one argument
# what a carried text's column holds in place of the text: a reader that predates the pieces shows it
MARKER = "[{} bytes of text carried in the blob store, read by lion.messages]"


class Sent(LionKhive):
    """The double, keeping each ops string it is sent."""

    def __init__(self) -> None:
        super().__init__(ACTOR)
        self.sent: list[str] = []

    async def exec(self, ops: str) -> list:
        self.sent.append(ops)
        return await super().exec(ops)

    def exec_sync(self, ops: str) -> list:
        self.sent.append(ops)
        return super().exec_sync(ops)


class LostOnce(Sent):
    """The double, losing the answer of the second call that writes message rows: the rows stand, and the
    client hears the error a dropped daemon answer gives."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    async def exec(self, ops: str) -> list:
        got = await super().exec(ops)
        if "lion.message_append" in ops:
            self.writes += 1
            if self.writes == 2:
                raise KhiveError(LOST)
        return got


@dataclass
class Recording(Khive):
    """The client over the built binary, keeping each ops string it sends."""

    sent: list[str] = field(default_factory=list)

    async def exec(self, ops: str) -> list:
        self.sent.append(ops)
        return await super().exec(ops)


def within(sent: list[str]) -> bool:
    return bool(sent) and max(len(s.encode()) for s in sent) <= ARGUMENT_BYTES


def count(sent: list[str], verb: str) -> int:
    return sum(s.count(f"{verb}(") for s in sent)


def a_run(store: Store, inputs: tuple[str, ...]):
    actor = Actor("clerk", store=store)
    turns = scripted(*TURNS)

    async def billed(messages):
        return Reply(await turns(messages), prompt_tokens=11, cost=0.001)

    request = RunRequest(inputs=inputs, emits=(Done,), max_turns=3)
    return asyncio.run(actor.run(Profile("clerk"), request, billed))


def take(k, exchanges: list[dict], sid: str = "s-big") -> tuple[int, int]:
    kw = {"source": "claude-code", "actor": ACTOR, "transcript": "/t/cc.jsonl", "why": "manual"}
    return asyncio.run(record_exchanges(k, session_id=sid, exchanges=exchanges, **kw))[1:]


def current(k, sid: str) -> int:
    """The generation of the CLI session's newest take: a harness row written by hand names it (D12)."""
    (got,) = asyncio.run(k.exec(f'lion.get(table="takes", id="{sid}")'))
    return got["generation"]


def opened(k, sid: str):
    asyncio.run(k.exec(f'lion.session_open(id="{sid}", source="lion1", actor={lit(ACTOR)})'))
    return k


def row(seq: int, text: str, **more) -> dict:
    return {"seq": seq, "role": "user", "kind": "input", "text": text, "turn": 0, **more}


def put(k, data: bytes) -> str:
    (got,) = asyncio.run(k.exec(f"blob.put(bytes={lit(base64.b64encode(data).decode())})"))
    return got["content_ref"]


def carried_append(sid: str, seq: int, manifest: dict, generation: int | None = None) -> str:
    return (
        f'lion.message_append_carried(session_id="{sid}", seq={seq}, role="user", kind="input", text="", '
        f"turn=0, carried={lit({'text': manifest})}"
        + (f", generation={generation})" if generation is not None else ")")
    )


def test_the_numbers_keep_a_row_and_a_piece_within_one_argument():
    # a piece's base64 and its call
    assert len(f'blob.put(bytes="{"A" * (4 * PIECE_BYTES // 3)}")') < ARGUMENT_BYTES
    # three fields at the threshold, inline or as their manifests, beside the row's other arguments
    assert len(lit({"size": 10**12, "pieces": [NOWHERE] * REFS_INLINE})) < INLINE_BYTES
    assert 3 * INLINE_BYTES + 4096 < ARGUMENT_BYTES
    # a read's answers under the daemon's 4 MiB budget for one call
    assert PIECES_PER_READ * 4 * PIECE_BYTES // 3 < 4 * 1024 * 1024
    assert len(BIG.encode()) == 308_000 > 300 * 1024 and len(lit(BIG)) > len(BIG.encode())


def test_a_300_kib_entry_is_carried_and_a_resume_reads_it_back_whole():
    k = Sent()
    store = Store(k)
    run = a_run(store, (BIG,))
    assert within(k.sent) and count(k.sent, "lion.message_append_carried") == 1
    held = list(k.lion.messages[run.id].values())
    (first,) = [m for m in held if m["kind"] == "input"]
    assert first["text"] == MARKER.format(len(BIG.encode())) and first["raw"] is None
    pieces = json.loads(first["carried"])
    assert set(pieces) == {"text", "raw"}
    assert count(k.sent, "blob.put") == len(pieces["text"]["pieces"]) + len(pieces["raw"]["pieces"])
    (got,) = [m for m in asyncio.run(store.messages(run.id)) if m["kind"] == "input"]
    assert got["text"] == BIG and json.loads(got["raw"])["content"] == BIG
    assert all(s.count("blob.get(") <= PIECES_PER_READ for s in k.sent)
    history = asyncio.run(store.history(run.id))
    assert [e.content for e in history] == [e.content for e in run.record]
    # the rows a small entry makes are inline, as before
    assert all(m["carried"] is None for m in held if m is not first)


def test_a_field_is_weighed_by_its_literal_not_its_bytes():
    # 39,000 bytes of UTF-8 fit under the threshold; the literal escapes each character to six bytes
    rain = "雨" * 13_000
    assert len(rain.encode()) < INLINE_BYTES < len(lit(rain))
    k = Sent()
    run = a_run(Store(k), (rain,))
    assert within(k.sent)
    (got,) = [m for m in k.lion.messages[run.id].values() if m["kind"] == "input"]
    assert "text" in json.loads(got["carried"])
    assert [e.content for e in asyncio.run(Store(k).history(run.id))] == [e.content for e in run.record]


def test_a_300_kib_exchange_is_carried_and_read_back_whole():
    k = Sent()
    assert take(k, BIG_EXCHANGES) == (3, 3)
    assert within(k.sent)
    assert asyncio.run(exchanges_of(k, "s-big")) == [{**e, "why": "manual"} for e in BIG_EXCHANGES]
    held = k.lion.messages["s-big"]
    assert [m["carried"] is not None for m in held.values()] == [False, True, False]
    # the double hashes the whole text, as the pack does: a take compares it with the prompt's (D12)
    assert held[1]["content_hash"] == hashlib.sha256(BIG.encode()).hexdigest()


def test_a_take_counting_a_carried_exchange_fetches_no_piece():
    k = Sent()
    take(k, BIG_EXCHANGES)
    k.sent.clear()
    assert take(k, BIG_EXCHANGES) == (3, 0)
    assert count(k.sent, "blob.get") == 0 and count(k.sent, "lion.message_append") == 0


def test_a_field_of_more_pieces_than_one_read_takes_is_read_in_several_calls():
    k = Sent()
    assert take(k, HUGE, "s-huge") == (1, 1)
    assert within(k.sent)
    k.sent.clear()
    assert asyncio.run(exchanges_of(k, "s-huge")) == [{**HUGE[0], "why": "manual"}]
    gets = [s.count("blob.get(") for s in k.sent if "blob.get(" in s]
    assert gets == [30, 30, 10]  # 35 pieces of the text, 35 of its raw


def test_a_field_past_the_listed_refs_is_indexed_and_read_back_whole():
    k = opened(Sent(), "s-index")
    asyncio.run(append_messages(k, "s-index", [row(0, INDEXED)]))
    assert within(k.sent)
    manifest = json.loads(k.lion.messages["s-index"][0]["carried"])["text"]
    assert set(manifest) == {"size", "index"} and manifest["size"] == len(INDEXED)
    assert manifest["index"]["size"] == 64 * (REFS_INLINE + 1)
    (got,) = asyncio.run(read_messages(k, "s-index"))
    assert got["text"] == INDEXED


def test_a_large_directive_is_carried_beside_a_large_text():
    # the directive travels as its own argument as well as inside the entry: a summary's text in both
    k = opened(Sent(), "s-directive")
    directive = json.dumps(
        {"op": "summarize", "refs": [], "text": "d" * 128 * 1024, "at": None, "seqs": None}
    )
    rows = [row(0, "t" * 128 * 1024, raw=None, directive=directive)]
    asyncio.run(append_messages(k, "s-directive", rows))
    assert within(k.sent)
    assert set(json.loads(k.lion.messages["s-directive"][0]["carried"])) == {"text", "directive"}
    (got,) = asyncio.run(read_messages(k, "s-directive"))
    assert (got["text"], got["directive"]) == (rows[0]["text"], directive)


def test_a_row_still_past_one_argument_is_refused_before_any_piece_and_the_rows_before_it_are_written():
    # an alias is never carried: past one argument with its large fields carried, or with none to carry
    for i, text in enumerate((BIG, "small")):
        k = opened(Sent(), "s-past")
        rows = [row(0, "first"), row(1, text, alias="a" * 140_000)]
        with pytest.raises(KhiveError, match=f"past the {ARGUMENT_BYTES} of one argument") as refused:
            asyncio.run(append_messages(k, "s-past", rows))
        assert refused.value.kind == "invalid" and count(k.sent, "blob.put") == 0, i
        assert [r["text"] for r in asyncio.run(read_messages(k, "s-past"))] == ["first"], i


def test_a_missing_piece_is_refused_by_name_on_write_and_on_read():
    k = Sent()
    take(k, BIG_EXCHANGES)
    with pytest.raises(KhiveError, match="piece 0") as refused:
        asyncio.run(k.exec(carried_append("s-big", 3, {"size": 5, "pieces": [NOWHERE]}, current(k, "s-big"))))
    assert refused.value.kind == "not_found" and 3 not in k.lion.messages["s-big"]
    gone = json.loads(k.lion.messages["s-big"][1]["carried"])["text"]["pieces"][-1]
    del k.lion.blobs[gone]
    with pytest.raises(KhiveError, match=gone) as missing:
        asyncio.run(exchanges_of(k, "s-big"))
    assert missing.value.kind == "not_found"


def test_the_inline_verb_refuses_carried_fields_and_the_whole_session_read_refuses_carried_rows():
    # a pack that predates the pieces would store the row without them; the verb of its own is the
    # fence, and a reader of the unpaged form would take a carried row's empty text for its content
    k = Sent()
    take(k, BIG_EXCHANGES)
    inline = carried_append("s-big", 3, {"size": 5, "pieces": [NOWHERE]}).replace(
        "message_append_carried", "message_append"
    )
    with pytest.raises(KhiveError, match="goes through lion.message_append_carried"):
        asyncio.run(k.exec(inline))
    with pytest.raises(KhiveError, match="read it with lion.messages"):
        asyncio.run(k.exec('lion.get(table="messages", id="s-big")'))
    take(k, [turn(0)], "s-small")
    (got,) = asyncio.run(k.exec('lion.get(table="messages", id="s-small")'))
    assert got["count"] == 1


def test_a_piece_that_does_not_fill_its_place_is_refused():
    k = opened(Sent(), "s-shape")
    ref = put(k, b"x" * 60)
    with pytest.raises(KhiveError, match="piece 0 holds 60 bytes where its place takes 50"):
        asyncio.run(k.exec(carried_append("s-shape", 0, {"size": 50, "pieces": [ref]})))
    with pytest.raises(KhiveError, match="100000 bytes are 2 pieces, not 1"):
        asyncio.run(k.exec(carried_append("s-shape", 0, {"size": 100_000, "pieces": [ref]})))
    with pytest.raises(KhiveError, match="indexed past that"):
        asyncio.run(
            k.exec(carried_append("s-shape", 0, {"size": 60, "index": {"size": 64, "pieces": [ref]}}))
        )
    with pytest.raises(KhiveError, match=f"an index of 64 bytes for {REFS_INLINE + 1} pieces"):
        index = {"size": 64, "pieces": [put(k, b"0" * 64)]}
        asyncio.run(
            k.exec(carried_append("s-shape", 0, {"size": (REFS_INLINE + 1) * PIECE_BYTES, "index": index}))
        )
    assert 0 not in k.lion.messages["s-shape"]


def test_a_character_a_join_cuts_reads_back_whole_and_bytes_ending_inside_one_are_refused():
    k = opened(Sent(), "s-cut")
    asyncio.run(append_messages(k, "s-cut", [row(0, CUT)]))
    (got,) = asyncio.run(read_messages(k, "s-cut"))
    assert got["text"] == CUT
    ref = put(k, "雨".encode()[:2])
    with pytest.raises(KhiveError, match="not UTF-8"):
        asyncio.run(k.exec(carried_append("s-cut", 1, {"size": 2, "pieces": [ref]})))


def test_a_session_past_the_page_budget_is_read_in_pages_and_whole():
    k = opened(Sent(), "s-page")
    rows = [row(i, f"{i:03d}" + "p" * 40_000) for i in range(60)]
    asyncio.run(append_messages(k, "s-page", rows))
    assert within(k.sent) and count(k.sent, "blob.put") == 0  # each under the threshold: inline
    k.sent.clear()
    got = asyncio.run(read_messages(k, "s-page"))
    assert [r["text"] for r in got] == [r["text"] for r in rows]
    assert count(k.sent, "lion.messages") > 1


def test_a_flush_of_small_entries_past_one_argument_splits_its_calls():
    k = Sent()
    run = a_run(Store(k), SMALL_INPUTS)
    appends = [s for s in k.sent if "lion.message_append(" in s]
    assert within(appends) and len(appends) > 1
    assert max(s.count("lion.message_append(") for s in appends) < 100
    assert count(appends, "lion.message_append") == len(run.record) and count(k.sent, "blob.put") == 0


def test_a_read_whose_page_does_not_move_is_refused(monkeypatch):
    k = opened(Sent(), "s-stuck")
    asyncio.run(append_messages(k, "s-stuck", [row(0, "x")]))
    real = k.lion.one

    def stuck(verb, p):
        got = real(verb, p)
        return {**got, "next_after": 0} if verb == "lion.messages" else got

    monkeypatch.setattr(k.lion, "one", stuck)
    with pytest.raises(KhiveError, match="did not move past seq 0"):
        asyncio.run(read_messages(k, "s-stuck"))


def test_a_piece_that_reads_short_is_refused():
    k = Sent()
    take(k, BIG_EXCHANGES)
    c = json.loads(k.lion.messages["s-big"][1]["carried"])
    k.lion.messages["s-big"][1]["carried"] = json.dumps(
        {**c, "raw": {**c["raw"], "size": c["raw"]["size"] + 1}}
    )
    with pytest.raises(KhiveError, match="seq 1: carried raw reads"):
        asyncio.run(exchanges_of(k, "s-big"))


def test_a_take_after_one_that_failed_part_way_writes_only_the_seqs_that_did_not_land():
    k = Sent()
    three = [turn(0), turn(1), turn(2)]
    take(k, three[:1], "s-gap")
    raw = row_json({**three[2], "why": "manual"}, ensure_ascii=False)
    landed = {"seq": 2, "role": "user", "kind": "exchange", "text": "prompt 2", "raw": raw, "turn": 2}
    landed["generation"] = current(k, "s-gap")
    asyncio.run(append_messages(k, "s-gap", [landed]))  # seq 2 landed, seq 1 did not
    assert take(k, three, "s-gap") == (3, 1)
    assert asyncio.run(exchanges_of(k, "s-gap")) == [{**e, "why": "manual"} for e in three]


def test_a_flush_after_a_failure_sends_the_missing_rows_and_refuses_a_held_row_it_did_not_write():
    k = Sent()
    store = Store(k)
    run = a_run(store, (BIG,))
    held = k.lion.messages[run.id]
    last = max(held)
    del held[last]  # the row a failed flush did not land
    run.store_flushed, run.store_unsure = 0, True
    k.sent.clear()
    assert asyncio.run(store.flush(run)) == 1  # every held row read back whole, the carried one too
    assert (
        run.store_unsure is False and sorted(held) == list(range(last + 1)) and count(k.sent, "blob.get") > 0
    )
    # an entry replayed from a row with a null turn holds None: its raw says null, its column the pack's 0
    null = next(seq for seq, m in held.items() if not m["carried"])
    run.record._entries[null] = replace(run.record.entries[null], turn=None)
    held[null].update(raw=store._row(run.record.entries[null])["raw"], turn=0)
    run.store_flushed, run.store_unsure = 0, True
    assert asyncio.run(store.flush(run)) == 0 and run.store_unsure is False
    big = next(seq for seq, m in held.items() if m["carried"])
    small = next(seq for seq, m in held.items() if not m["carried"])
    c = json.loads(held[big]["carried"])
    swapped = json.dumps({**c, "text": c["raw"]})  # pieces that read back whole, as another text
    cases = (
        (small, "text", "foreign"),
        (small, "parent_seq", 99),
        (big, "turn", 99),
        (big, "carried", swapped),
    )
    for seq, name, value in cases:
        was = held[seq][name]
        held[seq][name] = value
        run.store_flushed, run.store_unsure = 0, True
        with pytest.raises(KhiveError, match=f"seq {seq} holds a row this run did not write"):
            asyncio.run(store.flush(run))
        assert run.store_unsure is True, name
        held[seq][name] = was


def test_a_flush_whose_answer_was_lost_is_finished_by_seq_and_the_run_still_closes():
    k = LostOnce()
    with pytest.raises(KhiveError, match=LOST):
        a_run(Store(k), SMALL_INPUTS)
    (run_id,) = k.lion.runs
    held = sorted(k.lion.messages[run_id])
    assert len(held) > len(SMALL_INPUTS) and held == list(range(len(held)))  # each entry once, none skipped
    assert k.lion.runs[run_id]["outcome"] == "stopped" and k.lion.sessions[run_id]["ended"] is not None


@pytest.mark.skipif(not os.environ.get("LION1_KHIVE"), reason="LION1_KHIVE names no built binary")
@pytest.mark.launches_cli("lion1-khive")
def test_carried_rows_against_the_built_binary(binary_dirs, monkeypatch):  # noqa: F811
    # the real pack reads each piece back, hashes the whole text and holds the shape; the reads go through
    # the daemon `exec` starts, whose answers for one call stop at 4 MiB; an actor that loads `lion` loads
    # `blob` beside it
    binary = os.environ["LION1_KHIVE"]
    monkeypatch.delenv("KHIVE_DB", raising=False)
    monkeypatch.delenv("KHIVE_NO_DAEMON")
    home, d = binary_dirs
    monkeypatch.setenv("HOME", str(home))
    (d / ".khive").mkdir()
    (d / ".khive" / "config.toml").write_text(routed_config(ACTOR, d, packs=("kg", "lion", "blob")))
    k = Recording(d, ACTOR, kkernel=binary)
    assert take(k, BIG_EXCHANGES) == (3, 3)
    assert asyncio.run(exchanges_of(k, "s-big")) == [{**e, "why": "manual"} for e in BIG_EXCHANGES]
    (page,) = asyncio.run(k.exec('lion.messages(session_id="s-big")'))
    big = page["rows"][1]
    assert big["text"] == MARKER.format(len(BIG.encode())) and big["raw"] is None
    assert set(json.loads(big["carried"])) == {"text", "raw"}
    assert big["content_hash"] == hashlib.sha256(BIG.encode()).hexdigest()
    assert page["rows"][0]["carried"] is None and page["next_after"] is None
    inline = carried_append("s-big", 3, {"size": 5, "pieces": [NOWHERE]}).replace(
        "message_append_carried", "message_append"
    )
    for ops, refusal in (
        (inline, "goes through lion.message_append_carried"),
        ('lion.get(table="messages", id="s-big")', "read it with lion.messages"),
        (carried_append("s-big", 3, {"size": 5, "pieces": [NOWHERE]}, current(k, "s-big")), "piece 0"),
        # an argument this pack does not know is refused, never dropped: the next field a client adds
        (inline.replace("carried=", "pieces_of="), "unknown field `pieces_of`"),
    ):
        with pytest.raises(KhiveError, match=refusal):
            asyncio.run(k.exec(ops))
    run = a_run(Store(k), (BIG,))
    assert [e.content for e in asyncio.run(Store(k).history(run.id))] == [e.content for e in run.record]
    opened(k, "s-shape")
    ref = put(k, b"x" * 60)
    for manifest, refusal in (
        ({"size": 50, "pieces": [ref]}, "piece 0 holds 60 bytes where its place takes 50"),
        ({"size": 100_000, "pieces": [ref]}, "100000 bytes are 2 pieces, not 1"),
        ({"size": 60, "index": {"size": 64, "pieces": [ref]}}, "indexed past that"),
        (
            {"size": (REFS_INLINE + 1) * PIECE_BYTES, "index": {"size": 64, "pieces": [put(k, b"0" * 64)]}},
            f"an index of 64 bytes for {REFS_INLINE + 1} pieces",
        ),
        ({"size": 2, "pieces": [put(k, "雨".encode()[:2])]}, "not UTF-8"),
    ):
        with pytest.raises(KhiveError, match=refusal):
            asyncio.run(k.exec(carried_append("s-shape", 0, manifest)))
    directive = json.dumps(
        {"op": "summarize", "refs": [], "text": "d" * 128 * 1024, "at": None, "seqs": None}
    )
    rows = [row(0, CUT), row(1, INDEXED), row(2, "t" * 128 * 1024, raw=None, directive=directive)]
    asyncio.run(append_messages(opened(k, "s-rows"), "s-rows", rows))
    got = asyncio.run(read_messages(k, "s-rows"))
    assert [(r["text"], r["directive"]) for r in got] == [(r["text"], r.get("directive")) for r in rows]
    assert take(k, HUGE, "s-huge") == (1, 1)  # its read is three calls, each under the 4 MiB budget
    assert asyncio.run(exchanges_of(k, "s-huge")) == [{**HUGE[0], "why": "manual"}]
    paged = [{**turn(i), "input": f"{i:03d}" + "p" * 40_000} for i in range(60)]
    assert take(k, paged, "s-page") == (60, 60)
    before = count(k.sent, "lion.messages")
    assert asyncio.run(exchanges_of(k, "s-page")) == [{**e, "why": "manual"} for e in paged]
    assert count(k.sent, "lion.messages") - before > 1
    assert within(k.sent)
    _stop(binary, d)
