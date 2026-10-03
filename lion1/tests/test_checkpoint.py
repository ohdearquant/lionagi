"""The checkpoint (ADR-0019): named state plus a folded view, taken with no model call, and its restore:
a run's record replayed as the next run's history. The take is pure and checks the kinds and whys it is
handed; `save` writes in the directory it is handed, never over a file; `to_history` refuses a record
that no longer folds to the view the checkpoint carries. The Claude Code session's take and restore are
in `tests/test_claude_code_checkpoint.py`."""

import dataclasses
import json
import re
import threading
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError, computed_field, model_serializer

from lion1 import checkpoint as module
from lion1.actor import Result
from lion1.checkpoint import (
    KINDS,
    LAZY,
    WHYS,
    CheckpointError,
    Var,
    entry_of,
    jsonable,
    listed,
    load,
    save,
    take,
    to_history,
)
from lion1.record import Directive, Entry, Kind, Record

ACTOR = "actor:one"
AT = datetime(2026, 9, 24, 10, 5, tzinfo=UTC)
ID = "ckpt-20260924-100500-actor-one"
FAILED = Result("note.get", False, error="no note 'absent'", reason="failed")


def var(name: str, kind: str, value, source: str = "declared", at: str = "", **kw) -> Var:
    return Var(name, kind, value, source, at, **kw)


def turn(i: int, text: str = "", kind: str = "person") -> dict:
    return {
        "turn": f"u{i}",
        "at": f"2026-09-24T10:00:{i:02d}Z",
        "kind": kind,
        "input": f"prompt {i}",
        "text": text,
    }


def row_of(e: Entry) -> dict:
    """One entry as a chat log writes it (ADR-0018/D1): a result as its fields, a directive as data."""
    content = dataclasses.asdict(e.content) if isinstance(e.content, Result) else e.content
    meta = dict(e.meta)
    if isinstance(d := meta.get("directive"), Directive):
        meta["directive"] = {"op": d.op, "refs": list(d.refs), "text": d.text, "at": d.at, "seqs": d.seqs}
    return {
        "seq": e.seq,
        "kind": e.kind.name,
        "name": e.name,
        "turn": e.turn,
        "alias": e.alias,
        "meta": meta,
        "content": content,
    }


def write_rows(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def a_record(op: str | None = "hide", text: str = "looked for a note") -> Record:
    """Five entries; the last, a SYSTEM entry, carries a directive over the middle two (or none)."""
    r = Record("r0")
    r.append(Kind.INPUT, "say hi back", inbound="actor:two")
    r.append(Kind.TEXT, '<lact g1>note.get(key="absent")</lact>', turn=1)
    r.append(Kind.RESULT, FAILED, alias="g1", execution=True)
    r.append(Kind.TEXT, "hi back", turn=2)
    if op is None:
        r.append(Kind.SYSTEM, "the view past its budget: nothing folded")
    else:
        d = Directive(op, ("_m1", "g1"), text=text if op == "summarize" else "", at=r.seq, seqs=(1, 2))
        r.append(Kind.SYSTEM, "the view past its budget: folded", directive=d)
    return r


@pytest.fixture
def plain(tmp_path) -> Path:
    """A test's own directory; its checkpoints go in `ckpt/`, which `save` makes."""
    return tmp_path


def test_the_lists_the_adr_names_and_the_two_ceilings():
    assert KINDS == ("fact", "posture")
    assert WHYS == ("threshold", "end", "manual")
    assert LAZY == 400
    c = take({}, [var("a", "fact", 1)], actor=ACTOR, why="manual", budget=1, now=AT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.why = "end"
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.vars[0].value = 2


def test_a_take_names_itself_keeps_var_order_and_lets_a_later_var_of_a_name_stand():
    given = [var("a", "fact", "1"), var("b", "fact", "2"), var("a", "fact", "3", "hook", "10:04")]
    c = take({"run": "r1", "cwd": "/w"}, given, actor=ACTOR, why="manual", budget=100, now=AT)
    assert c.id == ID and c.taken_at == "2026-09-24T10:05:00+00:00" and c.why == "manual"
    assert c.source == {"run": "r1", "cwd": "/w"}  # the source as given: no kind is guessed
    assert [(v.name, v.value, v.source, v.at) for v in c.vars] == [
        ("a", "3", "hook", "10:04"),
        ("b", "2", "declared", ""),
    ]
    assert c.view == {"budget": 100, "kept": [], "hidden": [], "summaries": []}
    # the slug: anything but a letter or a digit becomes one dash, none at the ends
    assert take({}, [], actor="actor:two:echo/", why="end", budget=1, now=AT).id.endswith("-actor-two-echo")
    # no clock given: now, with a zone
    fresh = take({}, [], actor=ACTOR, why="manual", budget=1)
    assert fresh.id.startswith("ckpt-") and datetime.fromisoformat(fresh.taken_at).tzinfo is not None


def test_a_take_refuses_a_why_or_a_kind_outside_its_lists_and_a_nameless_actor():
    with pytest.raises(CheckpointError, match="why 'later': one of threshold, end, manual"):
        take({}, [], actor=ACTOR, why="later", budget=1)
    with pytest.raises(CheckpointError, match="no actor: a checkpoint is the actor's"):
        take({}, [], actor="", why="manual", budget=1)
    with pytest.raises(CheckpointError, match="var 'v': kind 'blob' is not one of fact, posture"):
        take({}, [var("v", "blob", {"id": "a1"})], actor=ACTOR, why="manual", budget=1)
    # the taker's own lists: a kind and a why off the defaults stand when named, and a kind named eager
    # is never made lazy; a hook's frame is the harness's to refuse, never the run's
    own = take(
        {},
        [var("w", "watch", "x" * (LAZY + 1)), var("d", "directive", "<system-reminder>r</system-reminder>")],
        actor=ACTOR,
        why="rotate",
        budget=1,
        kinds=("watch", "directive"),
        whys=("rotate",),
        eager=("watch",),
    )
    assert [(v.name, v.size) for v in own.vars] == [("w", None), ("d", None)]
    with pytest.raises(CheckpointError, match="kind 'fact' is not one of watch, directive"):
        take(
            {},
            [var("f", "fact", 1)],
            actor=ACTOR,
            why="rotate",
            budget=1,
            kinds=("watch", "directive"),
            whys=("rotate",),
        )
    long = take(
        {}, [var("w", "watch", "x" * (LAZY + 1))], actor=ACTOR, why="w", budget=1, kinds=["watch"], whys=["w"]
    )
    assert long.vars[0].size == LAZY + 1  # the control: the same var, not named eager, is lazy


def test_a_long_var_is_its_name_size_and_where_it_loads_from():
    lanes = [{"kind": "task", "id": f"t{i}", "title": "placeholder lane"} for i in range(20)]
    given = [
        var("posture", "posture", "q" * (LAZY + 1), "notes", "7", ref='note.get(key="posture")'),
        var("lanes", "fact", lanes, "khive"),
        var("short", "fact", "s" * LAZY),
        var("harvested", "fact", None, "transcript", size=1234),
    ]
    c = take({}, given, actor=ACTOR, why="manual", budget=100, now=AT)
    by = {v.name: v for v in c.vars}
    # a ref names the store that keeps the value; without one the checkpoint file keeps it
    assert (by["posture"].value, by["posture"].size) == (None, LAZY + 1)
    assert by["lanes"].value == lanes and by["lanes"].size == len(json.dumps(lanes, ensure_ascii=False))
    assert by["short"].size is None
    assert by["harvested"] == given[3]  # a size already set is a harvester's word: not recomputed


def test_entry_of_reads_a_log_row_back_as_its_entry_and_skips_a_row_of_another_kind():
    assert entry_of({"kind": "USAGE", "seq": 9, "content": {"cost": 1}}) is None
    row = {
        "seq": 2,
        "kind": "RESULT",
        "name": "_r2",
        "turn": 1,
        "alias": "g1",
        "meta": {"execution": True, "inbound": "actor:two", "origin": "o", "reply": "r", "cost": 3},
        "content": dataclasses.asdict(FAILED),
    }
    e = entry_of(row)
    kept = {"execution": True, "inbound": "actor:two", "origin": "o", "reply": "r"}
    assert e == Entry(2, Kind.RESULT, FAILED, alias="g1", turn=1, meta=kept) and isinstance(e.content, Result)
    # a result's content that has no spec is not a Result, and the defaults stand for what a row lacks
    assert entry_of({"seq": 0, "kind": "RESULT", "content": "plain"}).content == "plain"
    assert entry_of({"seq": 0, "kind": "RESULT", "content": {"value": 1}}).content == {"value": 1}
    e = entry_of({"seq": 0, "kind": "INPUT", "content": "hi", "meta": None})
    assert (e.alias, e.turn, e.meta) == (None, 0, {})
    # a directive as data: pinned, or from a log written before pins existed
    pinned = {"op": "hide", "refs": ["_m1", "g1"], "text": "", "at": 4, "seqs": [1, 2]}
    d = entry_of({"seq": 4, "kind": "SYSTEM", "content": "folded", "meta": {"directive": pinned}})
    assert d.meta == {"directive": Directive("hide", ("_m1", "g1"), text="", at=4, seqs=(1, 2))}
    older = {"op": "summarize", "refs": ["_m1"], "text": "s"}
    o = entry_of(
        {"seq": 4, "kind": "RESULT", "content": {"spec": "context", "ok": True}, "meta": {"directive": older}}
    )
    assert o.meta["directive"] == Directive("summarize", ("_m1",), text="s", at=0, seqs=None)
    # the versions a result read its notes at come back as written; another shape under the key does not
    read = {"seq": 5, "kind": "RESULT", "content": {"spec": "look", "ok": True}}
    assert entry_of({**read, "meta": {"notes": {"cursor": 3, "spend": 1}}}).meta == {
        "notes": {"cursor": 3, "spend": 1}
    }
    for odd in ([3], {}, {"cursor": "3"}, {"cursor": True}, {4: 1}, "cursor"):
        assert entry_of({**read, "meta": {"notes": odd}}).meta == {}, odd


def test_a_residents_take_folds_its_record_by_name_and_carries_none_of_its_text(tmp_path):
    rec = a_record("summarize")
    path = write_rows(tmp_path / "r0.jsonl", map(row_of, rec))
    source = {"record": str(path), "run": "r0"}
    c = take(
        source,
        [var("last_wake", "fact", "10:04")],
        actor="actor:two:echo",
        why="end",
        budget=100,
        record=rec,
        now=AT,
    )
    assert c.id == "ckpt-20260924-100500-actor-two-echo"
    assert {k: v for k, v in c.source.items() if k != "digest"} == {
        "record": str(path),
        "run": "r0",
        "kind": "resident",
        "seq": 5,
        "fold_inputs": False,
    }
    assert c.view == {
        "budget": 100,
        "kept": [{"turn": "_in0", "at": 0}, {"turn": "_m3", "at": 3}, {"turn": "_sys4", "at": 4}],
        "hidden": ["_m1", "_r2"],
        "summaries": [{"before": "_m1", "text": "looked for a note"}],
    }
    held = json.dumps(dataclasses.asdict(c))
    assert (
        "no note 'absent'" not in held and "hi back" not in held
    )  # the record stays where source.record points


def test_fold_inputs_lets_the_input_fold_and_the_replay_honours_it(plain):
    rec = Record("r1")
    rec.append(Kind.INPUT, "first")
    rec.append(Kind.TEXT, "then")
    rec.append(Kind.SYSTEM, "folded", directive=Directive("hide", ("_in0", "_m1"), at=2, seqs=(0, 1)))
    path = write_rows(plain / "r1.jsonl", map(row_of, rec))
    kept = take({"record": str(path)}, [], actor=ACTOR, why="threshold", budget=10, record=rec, now=AT)
    folded = take(
        {"record": str(path)},
        [],
        actor=ACTOR,
        why="threshold",
        budget=10,
        record=rec,
        fold_inputs=True,
        now=AT,
    )
    assert kept.view["kept"] == [{"turn": "_in0", "at": 0}, {"turn": "_sys2", "at": 2}] and kept.view[
        "hidden"
    ] == ["_m1"]
    assert folded.view["kept"] == [{"turn": "_sys2", "at": 2}] and folded.view["hidden"] == ["_in0", "_m1"]
    assert (kept.source["fold_inputs"], folded.source["fold_inputs"]) == (False, True)
    assert len(to_history(kept)) == len(to_history(folded)) == 3


def test_to_history_replays_the_record_to_the_seq_of_the_take_each_row_back_as_its_entry(plain):
    rec = a_record("hide")
    path = write_rows(plain / "r0.jsonl", map(row_of, rec))
    c = take({"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=rec, now=AT)
    # the log as the run left it: a blank line and a usage row among the rows, a row appended after the take
    rows = [json.dumps(row_of(e)) for e in rec]
    usage = json.dumps({"kind": "USAGE", "seq": 2, "content": {"cost": 0.1}})
    later = json.dumps(row_of(Entry(5, Kind.INPUT, "one more")))
    path.write_text("\n".join([*rows[:3], "", usage, *rows[3:], later]) + "\n")
    history = to_history(c)
    assert history == tuple(rec) and len(history) == 5
    assert isinstance(history[2].content, Result) and history[2].content == FAILED
    assert history[4].meta["directive"] == rec.by_seq(4).meta["directive"] and history[0].meta == {
        "inbound": "actor:two"
    }
    save(c, plain / "ckpt")
    assert to_history(load(plain / "ckpt")) == history


def test_to_history_refuses_a_first_prompt_checkpoint_a_missing_record_and_a_record_that_changed(plain):
    rec = a_record("hide")
    path = write_rows(plain / "r0.jsonl", map(row_of, rec))
    c = take({"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=rec, now=AT)
    for kind in ("claude-code", None):
        first = take({"kind": kind, "transcript": "/t.jsonl"}, [], actor=ACTOR, why="end", budget=100, now=AT)
        with pytest.raises(CheckpointError, match=f"holds no run's record \\(its source is {kind!r}\\)"):
            to_history(first)
    gone = dataclasses.replace(c, source={**c.source, "record": str(plain / "gone.jsonl")})
    with pytest.raises(CheckpointError, match="its record .* cannot be read"):
        to_history(gone)
    rows = [row_of(e) for e in rec]
    for held, said in (
        (rows[:4], "holds seqs 4 of 5 in order; nothing was replayed"),
        ([rows[0], rows[2], rows[1], rows[3], rows[4]], "holds seqs 5 of 5 in order"),
    ):
        write_rows(path, held)
        with pytest.raises(CheckpointError, match=said):
            to_history(c)
    twice = [dict(r) for r in rows]
    twice[3]["alias"] = "g1"
    write_rows(path, twice)
    with pytest.raises(CheckpointError, match="does not replay: alias 'g1' already names _r2"):
        to_history(c)
    write_rows(path, map(row_of, a_record(None)))
    with pytest.raises(
        CheckpointError, match=r"no longer folds to the view of .* \(kept differ\); nothing was replayed"
    ):
        to_history(c)
    summarized = take(
        {"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=a_record("summarize"), now=AT
    )
    write_rows(path, map(row_of, a_record("summarize", text="another summary")))
    with pytest.raises(CheckpointError, match=r"\(summaries differ\)"):
        to_history(summarized)
    write_rows(path, map(row_of, a_record("summarize")))
    assert len(to_history(summarized)) == 5  # the control: the record as folded replays


def test_a_record_whose_rows_were_rewritten_is_refused_though_it_folds_to_the_same_view(plain):
    # the replay compared the folded view by name alone: a row's text rewritten in place (the same
    # kinds, the same names, the same fold) replayed as what it now said
    rec = a_record("hide")
    path = write_rows(plain / "r0.jsonl", map(row_of, rec))
    c = take({"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=rec, now=AT)
    assert re.fullmatch(r"[0-9a-f]{64}", c.source["digest"])
    rows = [row_of(e) for e in rec]
    for seq, field, said in (
        (0, None, "say hi to someone else"),
        (2, "error", "no note 'present'"),
        (3, None, "hi, someone else"),
    ):
        changed = [dict(r) for r in rows]
        changed[seq]["content"] = said if field is None else {**changed[seq]["content"], field: said}
        write_rows(path, changed)
        with pytest.raises(CheckpointError, match=r"its rows were rewritten\); nothing was replayed"):
            to_history(c)
    write_rows(path, rows)
    assert to_history(c) == tuple(rec)  # the control: the rows as taken replay
    bare = dataclasses.replace(c, source={k: v for k, v in c.source.items() if k != "digest"})
    with pytest.raises(CheckpointError, match="carries no digest of its record; nothing was replayed"):
        to_history(bare)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"seq": 1, "content": "x"}',
        "5",
        '{"seq": 1, "kind": "RESULT", "content": {"spec": "x", "ok": true, "extra": 1}}',
        '{"seq": 1, "kind": "SYSTEM", "content": "f", "meta": {"directive": {"refs": []}}}',
    ],
)
def test_a_line_that_is_not_a_record_row_is_named_and_nothing_replays(plain, raw):
    rec = a_record("hide")
    path = write_rows(plain / "r0.jsonl", map(row_of, rec))
    c = take({"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=rec, now=AT)
    path.write_text(json.dumps(row_of(rec.by_seq(0))) + "\n" + raw + "\n")
    with pytest.raises(CheckpointError, match=r"line 2 is not a record row \(.+\); nothing was replayed"):
        to_history(c)


def test_a_row_whose_text_carries_a_separator_is_one_row_and_a_var_with_one_round_trips(plain):
    odd = "one\u2028two\x85three\u2029four"  # legal unescaped in a JSON string; str.splitlines breaks at each
    rec = Record("r0")
    rec.append(Kind.INPUT, odd, inbound="actor:two")
    rec.append(Kind.TEXT, "hi back", turn=1)
    path = plain / "r0.jsonl"
    path.write_text("".join(json.dumps(row_of(e), ensure_ascii=False) + "\n" for e in rec))
    text = path.read_text()
    assert odd in text and text.count("\n") == 2 and len(text.splitlines()) == 5
    c = take(
        {"record": str(path)},
        [var("note", "fact", odd)],
        actor=ACTOR,
        why="end",
        budget=100,
        record=rec,
        now=AT,
    )
    history = to_history(c)
    assert history == tuple(rec) and history[0].content == odd
    save(c, plain / "ckpt")
    again = load(plain / "ckpt")
    assert again.id == c.id and again.vars[0].value == odd
    assert to_history(again) == history


def test_a_row_cut_inside_a_character_past_the_take_is_never_decoded_and_one_inside_it_is_named(plain):
    # the record was decoded whole before the cutoff: a write cut inside a multi-byte character after
    # the take raised UnicodeDecodeError, which is not the CheckpointError a restore's caller reads
    rec = Record("r0")
    rec.append(Kind.INPUT, "say hi back", inbound="actor:two")
    rec.append(Kind.TEXT, "hi back ☃", turn=1)
    rows = [json.dumps(row_of(e), ensure_ascii=False).encode() for e in rec]
    path = plain / "r0.jsonl"
    path.write_bytes(b"\n".join(rows) + b"\n\xff not text\n" + "☃".encode()[:2])
    c = take({"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=rec, now=AT)
    save(c, plain / "ckpt")
    assert to_history(load(plain / "ckpt")) == tuple(rec)
    path.write_bytes(rows[0] + b"\n" + rows[1][:-5] + b"\xe2\x98")
    with pytest.raises(
        CheckpointError, match=r"r0\.jsonl line 2 is not a record row \(.*utf-8.*\); nothing was replayed"
    ):
        to_history(c)


def test_save_lands_the_file_names_it_latest_and_never_overwrites(plain):
    view = {"budget": 100, "kept": [turn(1, "one")], "hidden": [], "summaries": []}
    c = take(
        {"transcript": "/t.jsonl"},
        [var("a", "fact", ["x", 1])],
        actor=ACTOR,
        why="manual",
        budget=100,
        view=view,
        now=AT,
    )
    ctx = plain / "ckpt"
    first = save(c, plain / "ckpt")
    assert first == ctx / f"{ID}.json" and json.loads((ctx / "latest.json").read_text()) == {"id": ID}
    assert load(plain / "ckpt") == c and load(plain / "ckpt", ID) == c
    doc = json.loads(first.read_text())
    assert doc["id"] == ID and doc["actor"] == ACTOR and doc["view"] == view
    assert doc["vars"] == [
        {
            "name": "a",
            "kind": "fact",
            "value": ["x", 1],
            "source": "declared",
            "at": "",
            "size": None,
            "ref": None,
        }
    ]
    second, third = save(c, plain / "ckpt"), save(c, plain / "ckpt")
    assert [p.stem for p in (second, third)] == [f"{ID}-02", f"{ID}-03"]
    assert json.loads(first.read_text()) == doc and json.loads(third.read_text())["id"] == f"{ID}-03"
    assert load(plain / "ckpt").id == f"{ID}-03" and load(plain / "ckpt", f"{ID}-02").id == f"{ID}-02"
    assert dataclasses.replace(load(plain / "ckpt"), id=ID) == c
    assert sorted(p.name for p in ctx.iterdir()) == [
        f"{ID}-02.json",
        f"{ID}-03.json",
        f"{ID}.json",
        "latest.json",
    ]


def test_a_taken_name_costs_a_probe_and_never_another_encoding_of_the_document(plain, monkeypatch):
    # each occupied name built the file's bytes again from scratch: the eighth save of one id in one
    # second encoded the document eight times, 36 over the eight
    c = take({}, [var("a", "fact", "x" * 300)], actor=ACTOR, why="manual", budget=100, now=AT)
    encoded: list[str] = []
    real = module._bytes
    monkeypatch.setattr(module, "_bytes", lambda doc: encoded.append(doc["id"]) or real(doc))
    counts, paths = [], []
    for _ in range(8):
        before = len(encoded)
        paths.append(save(c, plain / "ckpt"))
        counts.append(len(encoded) - before)
    assert counts == [1] * 8 and encoded == [ID] * 8
    assert [p.stem for p in paths] == [ID, *(f"{ID}-{n:02d}" for n in range(2, 9))]
    for p in paths:  # each file the bytes an encoding under its own name gives, its id that name
        assert p.read_bytes() == real({**dataclasses.asdict(c), "id": p.stem})
        assert load(plain / "ckpt", p.stem) == dataclasses.replace(c, id=p.stem)
    assert load(plain / "ckpt").id == f"{ID}-08"


def test_load_says_what_is_missing_or_malformed_and_takes_only_a_name(plain):
    with pytest.raises(CheckpointError, match="latest.json is absent"):
        load(plain / "ckpt")
    ctx = plain / "ckpt"
    ctx.mkdir(parents=True)
    for latest in ("not json", '{"nope": 1}', "[1]"):
        (ctx / "latest.json").write_text(latest)
        with pytest.raises(CheckpointError, match="latest.json does not name a checkpoint"):
            load(plain / "ckpt")
    (ctx / "latest.json").write_text('{"id": "../../etc/passwd"}')
    with pytest.raises(CheckpointError, match="checkpoint id '../../etc/passwd' is not a name in"):
        load(plain / "ckpt")
    for bad in (7, "", ".hidden", "a/b", f"{ID}\n"):
        with pytest.raises(CheckpointError, match="is not a name in"):
            load(plain / "ckpt", bad)
    with pytest.raises(CheckpointError, match="no checkpoint 'ckpt-absent' in"):
        load(plain / "ckpt", "ckpt-absent")


def test_save_and_load_refuse_a_why_or_a_kind_off_the_lists_and_name_the_value(plain):
    # `save` and `load` checked no kind and no why: a file edited to hold another kind loaded, and the
    # restore left a value of that kind out of the prompt without a word
    c = take(
        {},
        [var("a", "fact", "one"), var("t", "posture", "two")],
        actor=ACTOR,
        why="manual",
        budget=100,
        now=AT,
    )
    whys, kinds = ", ".join(WHYS), ", ".join(KINDS)
    blob = var("v", "blob", {"id": "a1"})
    ctx = plain / "ckpt"
    for bad, said in (
        (dataclasses.replace(c, why="later"), f"{ID}: why 'later' is not one of {whys}; nothing was written"),
        (
            dataclasses.replace(c, vars=(*c.vars, blob)),
            f"{ID}: var 'v' has kind 'blob', not one of {kinds}; nothing was written",
        ),
    ):
        with pytest.raises(CheckpointError, match=re.escape(said)):
            save(bad, plain / "ckpt")
    assert not ctx.exists()
    path = save(c, plain / "ckpt")
    assert load(plain / "ckpt") == c  # the control: the file as written loads
    doc = json.loads(path.read_text())
    for edit, said in (
        ({"why": "later"}, f"{ID}: why 'later' is not one of {whys}; nothing was loaded"),
        (
            {"vars": [doc["vars"][0], {**doc["vars"][1], "kind": "blob"}]},
            f"{ID}: var 't' has kind 'blob', not one of {kinds}; nothing was loaded",
        ),
    ):
        path.write_text(json.dumps({**doc, **edit}))
        with pytest.raises(CheckpointError, match=re.escape(said)):
            load(plain / "ckpt")
    # the taker's own lists: a file of another vocabulary saves and loads under it, and only under it
    other = dataclasses.replace(c, why="later", vars=(*c.vars, blob))
    mine = {"kinds": (*KINDS, "blob"), "whys": (*WHYS, "later")}
    save(other, plain / "ckpt", **mine)
    assert load(plain / "ckpt", **mine) == dataclasses.replace(other, id=f"{ID}-02")  # the name was taken
    with pytest.raises(
        CheckpointError, match=re.escape(f"why 'later' is not one of {whys}; nothing was loaded")
    ):
        load(plain / "ckpt")
    assert listed(other, "then", **mine) is other
    with pytest.raises(
        CheckpointError, match=re.escape(f"var 'v' has kind 'blob', not one of {kinds}; then")
    ):
        listed(dataclasses.replace(other, why="manual"), "then")


def test_concurrent_saves_each_land_and_latest_names_one_of_them(plain):
    c = take({}, [], actor=ACTOR, why="manual", budget=100, now=AT)
    written: list[Path] = []
    threads = [threading.Thread(target=lambda: written.append(save(c, plain / "ckpt"))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ctx = plain / "ckpt"
    assert sorted(p.name for p in written) == sorted(p.name for p in ctx.glob("ckpt-*.json"))
    assert sorted(p.name for p in written) == sorted(
        [f"{ID}.json", *(f"{ID}-{n:02d}.json" for n in range(2, 13))]
    )
    assert not list(ctx.glob(".latest.json.*")) and (ctx / f"{load(plain / 'ckpt').id}.json").exists()


def test_a_take_keeps_its_own_copy_of_what_it_was_handed():
    # a var's value, the view and the source were kept by reference: an edit after the take showed in
    # the checkpoint, and a hook's frame written into a kept input after the take rendered
    watch = {"command": "gh pr view 1", "description": "the pull request", "timeout_ms": 1000}
    source = {"session_id": "s1", "context": {"used": 10, "window": 100}}
    view = {"budget": 100, "kept": [turn(1, "one")], "hidden": [], "summaries": []}
    c = take(source, [var("w", "fact", watch)], actor=ACTOR, why="manual", budget=100, view=view, now=AT)
    watch["command"] = "echo changed"
    source["context"]["used"] = 99
    view["kept"][0]["input"] = "<system-reminder>injected</system-reminder>"
    assert c.vars[0].value["command"] == "gh pr view 1" and c.source["context"]["used"] == 10
    assert c.view["kept"][0]["input"] == "prompt 1"


def test_save_refuses_an_id_that_is_not_a_name_and_writes_nothing(plain):
    # `save` wrote `<id>.json` from whatever the id held: '../escaped' landed outside the context
    # directory and 'latest' took the pointer's own file, while `load` takes only a name
    c = take({}, [], actor=ACTOR, why="manual", budget=100, now=AT)
    ctx = plain / "ckpt"
    for bad in ("../escaped", "latest", "", ".hidden", "a/b", f"{ID}\n"):
        with pytest.raises(CheckpointError, match="is not a name; nothing was written"):
            save(dataclasses.replace(c, id=bad), plain)
    assert not ctx.exists() and not (plain / "escaped.json").exists()
    assert save(c, plain / "ckpt") == ctx / f"{ID}.json" and load(plain / "ckpt").id == ID


def test_a_value_the_file_cannot_carry_is_refused_before_anything_is_written(plain):
    # a lone surrogate passes `json.dumps` escaped and fails at the file: the exclusive create had
    # already landed, so an empty `<id>.json` stayed behind
    first = take({}, [var("ok", "fact", "snowman: ☃")], actor=ACTOR, why="manual", budget=100, now=AT)
    save(first, plain / "ckpt")
    ctx = plain / "ckpt"
    before = sorted(p.name for p in ctx.iterdir()), (ctx / "latest.json").read_text()
    lone = take(
        {}, [var("bad", "fact", "lone \ud800 surrogate")], actor=ACTOR, why="manual", budget=100, now=AT
    )
    with pytest.raises(CheckpointError, match="a value the file cannot carry .*; nothing was written"):
        save(lone, plain / "ckpt")
    assert (sorted(p.name for p in ctx.iterdir()), (ctx / "latest.json").read_text()) == before
    assert load(plain / "ckpt").vars[0].value == "snowman: ☃"


class Book(BaseModel):
    title: str
    year: int


class Loose(BaseModel):
    v: object


def test_a_model_value_is_written_as_its_fields_and_one_json_cannot_hold_as_its_str(plain):
    # a var's value that is a model was written, shown and sized as its str, which a restore cannot
    # read a field of
    book = Book(title="Dune", year=1965)
    odd = Loose(v=types.SimpleNamespace(x=1))  # pydantic has no JSON form for the namespace
    assert jsonable(book) == {"title": "Dune", "year": 1965}
    assert jsonable(odd) == str(odd) == "v=namespace(x=1)" and jsonable(AT) == str(AT)
    c = take({}, [var("value.b", "fact", book)], actor=ACTOR, why="manual", budget=100, now=AT)
    save(c, plain / "ckpt")
    assert load(plain / "ckpt").vars[0].value == {"title": "Dune", "year": 1965}


class Shut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str

    @computed_field
    @property
    def upper(self) -> str:
        return self.title.upper()


def test_a_computed_field_is_written_as_a_key_that_a_model_forbidding_extras_refuses():
    # a replay validates the dict into the model: a computed field's key is extra input there
    written = jsonable(Shut(title="dune"))
    assert written == {"title": "dune", "upper": "DUNE"}
    with pytest.raises(ValidationError, match="upper\n  Extra inputs are not permitted"):
        Shut.model_validate(written)
    assert Shut.model_validate({"title": "dune"}).upper == "DUNE"


class Open(Shut):
    model_config = ConfigDict(extra="allow")


def test_a_computed_field_is_written_as_a_key_that_a_model_allowing_extras_keeps_as_extra_data():
    back = Open.model_validate(jsonable(Open(title="dune")))
    assert back.model_extra == {"upper": "DUNE"} and back.title == "dune" and back.upper == "DUNE"


class Tags(RootModel[list[str]]):
    pass


class Mark(BaseModel):
    title: str

    @model_serializer
    def as_title(self) -> str:
        return self.title


def test_a_model_whose_dump_is_not_an_object_is_written_as_that_dump(plain):
    # a root model dumps its root and a serializer may return a scalar: the file holds that JSON, and a
    # restore reads it back as it was written
    tags, mark = Tags(["Dune", "sf"]), Mark(title="Dune")
    assert (jsonable(tags), jsonable(mark)) == (["Dune", "sf"], "Dune")
    c = take(
        {},
        [var("value.t", "fact", tags), var("value.m", "fact", mark)],
        actor=ACTOR,
        why="manual",
        budget=100,
        now=AT,
    )
    save(c, plain / "ckpt")
    assert {v.name: v.value for v in load(plain / "ckpt").vars} == {
        "value.t": ["Dune", "sf"],
        "value.m": "Dune",
    }
