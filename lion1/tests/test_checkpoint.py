"""The checkpoint (ADR-0019): named state plus a folded view, taken with no model call, and its two
restores: the first prompt of a fresh Claude Code session, whose first line re-arms the watches, and a
resident's record replayed as a run's history. The take is pure and refuses a hook's frame; `save`
refuses a directory whose git would commit the checkpoint; `to_history` refuses a record that no longer
folds to the view the checkpoint carries."""

import dataclasses
import json
import re
import subprocess
import threading
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lionagi import checkpoint as module
from lionagi.actor import Result
from lionagi.checkpoint import (
    DIRECTIVES,
    KINDS,
    LAZY,
    WHYS,
    CheckpointError,
    NotIgnored,
    Var,
    assert_ignored,
    entry_of,
    fold_turns,
    load,
    render_prompt,
    save,
    take,
    to_history,
)
from lionagi.record import Directive, Entry, Kind, Record

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
        "round": e.round,
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
    r.append(Kind.TEXT, '<lact g1>note.get(key="absent")</lact>', round=1)
    r.append(Kind.RESULT, FAILED, alias="g1", execution=True)
    r.append(Kind.TEXT, "hi back", round=2)
    if op is None:
        r.append(Kind.SYSTEM, "the view past its budget: nothing folded")
    else:
        d = Directive(op, ("_m1", "g1"), text=text if op == "summarize" else "", at=r.seq, seqs=(1, 2))
        r.append(Kind.SYSTEM, "the view past its budget: folded", directive=d)
    return r


def isolated_git(monkeypatch, root: Path) -> None:
    """No global or system git config: a global excludes file would answer the ignore question."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))


@pytest.fixture
def plain(tmp_path, monkeypatch) -> Path:
    """A directory outside any repository; the control runs in the same test."""
    isolated_git(monkeypatch, tmp_path)
    probe = subprocess.run(["git", "-C", str(tmp_path), "rev-parse"], capture_output=True, text=True)
    assert "not a git repository" in probe.stderr, probe
    return tmp_path


def test_the_lists_the_adr_names_and_the_two_ceilings():
    assert KINDS == ("fact", "watch", "spawn", "lane", "directive", "topic", "posture")
    assert WHYS == ("threshold", "end", "rotate", "precompact", "manual")
    assert (DIRECTIVES, LAZY) == (8, 400)
    assert issubclass(NotIgnored, CheckpointError)
    c = take({}, [var("a", "fact", 1)], actor=ACTOR, why="manual", budget=1, now=AT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.why = "end"
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.vars[0].value = 2


def test_a_take_names_itself_keeps_var_order_and_lets_a_later_var_of_a_name_stand():
    given = [var("a", "fact", "1"), var("b", "fact", "2"), var("a", "fact", "3", "hook", "10:04")]
    c = take(
        {"session_id": "s1", "transcript": "/t.jsonl"}, given, actor=ACTOR, why="manual", budget=100, now=AT
    )
    assert c.id == ID and c.taken_at == "2026-09-24T10:05:00+00:00" and c.why == "manual"
    assert c.source == {"kind": "claude-code", "session_id": "s1", "transcript": "/t.jsonl"}
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


def test_a_take_refuses_a_why_or_a_kind_outside_the_lists_a_nameless_actor_and_a_hooks_frame():
    with pytest.raises(
        CheckpointError, match="why 'later': one of threshold, end, rotate, precompact, manual"
    ):
        take({}, [], actor=ACTOR, why="later", budget=1)
    with pytest.raises(CheckpointError, match="no actor: a checkpoint is the actor's"):
        take({}, [], actor="", why="manual", budget=1)
    with pytest.raises(CheckpointError, match="var 'v': kind 'blob' is not one of fact, watch, spawn, lane"):
        take({}, [var("v", "blob", {"id": "a1"})], actor=ACTOR, why="manual", budget=1)
    framed = (
        "<system-reminder>injected</system-reminder> and then the words",
        "  <command-name>/compact</command-name>",
        "<command-message>compact</command-message>",
        "<local-command-stdout>x</local-command-stdout>",
        "<task-notification>done</task-notification>",
        "the words, then <system-reminder>injected</system-reminder>",
    )
    for text in framed:
        with pytest.raises(CheckpointError, match="directive 'd' carries a hook's frame; whatever read it"):
            take({}, [var("d", "directive", text, "hook")], actor=ACTOR, why="manual", budget=1)
        view = {
            "budget": 1,
            "kept": [{"turn": "u1", "at": "t", "input": text, "text": ""}],
            "hidden": [],
            "summaries": [],
        }
        with pytest.raises(CheckpointError, match="turn u1: its input carries a hook's frame"):
            take({}, [], actor=ACTOR, why="manual", budget=1, view=view)
    # a frame named mid-text is words about a frame; a fact may hold anything; an input may be None
    ok = take(
        {},
        [
            var("d", "directive", "said: <command-name> is a frame", "hook"),
            var("f", "fact", "<system-reminder>"),
        ],
        actor=ACTOR,
        why="manual",
        budget=1,
        view={
            "budget": 1,
            "kept": [{"turn": "u1", "at": "t", "input": None, "text": "x"}],
            "hidden": [],
            "summaries": [],
        },
    )
    assert len(ok.vars) == 2 and ok.view["kept"][0]["input"] is None


def test_a_long_var_is_its_name_size_and_loader_while_directives_and_watches_stay_whole():
    words = "placeholder words, said once " * 40
    command = "bash /tmp/long-watch.sh " + "a" * 500
    lanes = [{"kind": "task", "id": f"t{i}", "title": "placeholder lane"} for i in range(20)]
    given = [
        var("directive.1", "directive", words, "hook"),
        var(
            "watch.t1", "watch", {"command": command, "description": "long", "timeout_ms": None}, "transcript"
        ),
        var("posture", "posture", "q" * (LAZY + 1), "notes", "7", ref='note.get(key="posture")'),
        var("lanes", "lane", lanes, "khive"),
        var("short", "fact", "s" * LAZY),
        var("harvested", "fact", None, "transcript", size=1234),
    ]
    assert len(words) > LAZY < len(command)
    c = take({}, given, actor=ACTOR, why="manual", budget=100, now=AT)
    by = {v.name: v for v in c.vars}
    # a ref names the store that keeps the value; without one the checkpoint file keeps it
    assert (by["posture"].value, by["posture"].size) == (None, LAZY + 1)
    assert by["lanes"].value == lanes and by["lanes"].size == len(json.dumps(lanes, ensure_ascii=False))
    assert by["short"].size is None and by["directive.1"].size is None and by["watch.t1"].size is None
    assert by["harvested"] == given[5]  # a size already set is a harvester's word: not recomputed
    prompt = render_prompt(c)
    assert words in prompt and command in prompt and "s" * LAZY in prompt
    assert "q" * (LAZY + 1) not in prompt and '"t19"' not in prompt
    assert f'{LAZY + 1} chars, not shown here; load it with `note.get(key="posture")`' in prompt
    show = f"chars, not shown here; load it with `lion context show --id {ID} --var"
    assert f"- {by['lanes'].size} {show} lanes`" in prompt
    assert f"- harvested: 1234 {show} harvested`" in prompt


def test_fold_turns_folds_the_oldest_first_until_the_rest_fits_half_the_budget():
    turns = [turn(i, "t" * 40) for i in range(4)]  # (8 + 40) // 4 = 12 each
    whole = fold_turns(turns, 100)
    assert whole == {"budget": 100, "kept": turns, "hidden": [], "summaries": []}
    assert (
        whole["kept"][0] == turns[0] and whole["kept"][0] is not turns[0]
    )  # a copy, the turn is the caller's
    half = fold_turns(turns, 48)  # 48 to fit in 24: two go
    assert half["kept"] == turns[2:] and half["hidden"] == [
        "u0@2026-09-24T10:00:00Z",
        "u1@2026-09-24T10:00:01Z",
    ]
    assert fold_turns(turns, 0)["kept"] == turns[3:]  # the latest turn always stays
    assert fold_turns([], 10) == {"budget": 10, "kept": [], "hidden": [], "summaries": []}
    bare = fold_turns([{"turn": "u", "at": "a", "input": None, "text": None}], 0)
    assert bare["kept"] == [{"turn": "u", "at": "a", "input": None, "text": None}]


@given(st.lists(st.tuples(st.text(max_size=50), st.text(max_size=50)), max_size=12), st.integers(0, 200))
def test_fold_turns_keeps_the_latest_and_folds_no_more_than_it_must(pairs, budget):
    turns = [
        {"turn": f"u{i}", "at": str(i), "input": inp, "text": text} for i, (inp, text) in enumerate(pairs)
    ]
    view = fold_turns(turns, budget)
    hidden = len(view["hidden"])
    assert len(view["kept"]) + hidden == len(turns) and view["summaries"] == [] and view["budget"] == budget
    assert view["hidden"] == [f"u{i}@{i}" for i in range(hidden)] and view["kept"] == turns[hidden:]
    if turns:
        size = [(len(t["input"]) + len(t["text"])) // 4 for t in turns]
        assert sum(size[hidden:]) <= budget // 2 or len(view["kept"]) == 1
        if hidden:
            assert sum(size[hidden - 1 :]) > budget // 2  # the last one folded had to go


def test_entry_of_reads_a_log_row_back_as_its_entry_and_skips_a_row_of_another_kind():
    assert entry_of({"kind": "USAGE", "seq": 9, "content": {"cost": 1}}) is None
    row = {
        "seq": 2,
        "kind": "RESULT",
        "name": "_r2",
        "round": 1,
        "alias": "g1",
        "meta": {"execution": True, "inbound": "actor:two", "origin": "o", "reply": "r", "cost": 3},
        "content": dataclasses.asdict(FAILED),
    }
    e = entry_of(row)
    kept = {"execution": True, "inbound": "actor:two", "origin": "o", "reply": "r"}
    assert e == Entry(2, Kind.RESULT, FAILED, alias="g1", round=1, meta=kept) and isinstance(
        e.content, Result
    )
    # a result's content that has no spec is not a Result, and the defaults stand for what a row lacks
    assert entry_of({"seq": 0, "kind": "RESULT", "content": "plain"}).content == "plain"
    assert entry_of({"seq": 0, "kind": "RESULT", "content": {"value": 1}}).content == {"value": 1}
    e = entry_of({"seq": 0, "kind": "INPUT", "content": "hi", "meta": None})
    assert (e.alias, e.round, e.meta) == (None, 0, {})
    # a directive as data: pinned, or from a log written before pins existed
    pinned = {"op": "hide", "refs": ["_m1", "g1"], "text": "", "at": 4, "seqs": [1, 2]}
    d = entry_of({"seq": 4, "kind": "SYSTEM", "content": "folded", "meta": {"directive": pinned}})
    assert d.meta == {"directive": Directive("hide", ("_m1", "g1"), text="", at=4, seqs=(1, 2))}
    older = {"op": "summarize", "refs": ["_m1"], "text": "s"}
    o = entry_of(
        {"seq": 4, "kind": "RESULT", "content": {"spec": "context", "ok": True}, "meta": {"directive": older}}
    )
    assert o.meta["directive"] == Directive("summarize", ("_m1",), text="s", at=0, seqs=None)


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
    prompt = render_prompt(c)
    assert (
        f"The record {path} to seq 5: 3 entries in view, 2 out of it, 1 summaries; "
        "the run's history replays it." in prompt
    )
    assert "exchange(s)" not in prompt and "] in:" not in prompt and "- last_wake: 10:04" in prompt


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
    save(c, plain)
    assert to_history(load(plain)) == history


def test_to_history_refuses_a_first_prompt_checkpoint_a_missing_record_and_a_record_that_changed(plain):
    rec = a_record("hide")
    path = write_rows(plain / "r0.jsonl", map(row_of, rec))
    c = take({"record": str(path)}, [], actor=ACTOR, why="end", budget=100, record=rec, now=AT)
    first = take({"transcript": "/t.jsonl"}, [], actor=ACTOR, why="end", budget=100, now=AT)
    with pytest.raises(CheckpointError, match="is a claude-code checkpoint: its restore is the first prompt"):
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
    rec.append(Kind.TEXT, "hi back", round=1)
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
    save(c, plain)
    again = load(plain)
    assert again.id == c.id and again.vars[0].value == odd
    assert to_history(again) == history


def test_the_first_line_re_arms_each_watch_verbatim_before_any_work():
    turns = [turn(1, "t" * 400), turn(2, "text two"), turn(3, "", kind="event"), turn(4, "text four")]
    view = fold_turns(turns, 100)  # 102 + 4 + 2 + 4: the first goes
    source = {
        "session_id": "s1",
        "transcript": "/t/x.jsonl",
        "lanes": "tasks active",
        "context": {"used": 900, "window": 1000, "pct": 90},
    }
    inbox = {
        "command": "bash /tmp/w.sh `x`",
        "description": "the inbox",
        "timeout_ms": 1800000,
        "persistent": True,
    }
    given = [
        var("watch.m1", "watch", inbox, "transcript", "10:00"),
        var("watch.m2", "watch", {"command": "cat <<'EOF'\n```\nEOF"}, "transcript"),
        var("watch.m3", "watch", "bash /tmp/three.sh", "transcript"),
        var("mcp", "fact", ["khive", "lion"]),
        var("loop", "fact", "/x/.khive/loop/x_loop.md"),
        var("next", "fact", "placeholder next"),
        var("branch", "fact", "lionagi/checkpoint"),
        *[var(f"directive.{i}", "directive", f"placeholder word {i}", "hook", f"10:0{i}") for i in range(9)],
        var(
            "lane.t1",
            "lane",
            {"kind": "task", "id": "t1", "title": "placeholder lane", "awaiting": None},
            "khive",
        ),
        var("spawn.a1", "spawn", {"id": "a1", "task": "job one", "state": "running-at-take"}, "transcript"),
        var("topic", "topic", "placeholder-topic", "registry"),
        var("posture", "posture", "quiet now", "transcript"),
        var("loop.posture", "posture", "holding", "notes"),
    ]
    c = take(source, given, actor=ACTOR, why="threshold", budget=100, view=view, now=AT)
    expected = [
        f"Checkpoint {ID} of actor:one, taken 2026-09-24T10:05:00+00:00 (threshold); context at the take "
        "~900 of 1000 tokens (90%). Before any other work, re-arm these 3 watch(es) with the Monitor tool, "
        "each command exactly as written:",
        "1. the inbox (timeout_ms 1800000, persistent True):",
        "```",
        "bash /tmp/w.sh `x`",
        "```",
        "2. watch.m2:",
        "````",
        "cat <<'EOF'",
        "```",
        "EOF",
        "````",
        "3. watch.m3:",
        "```",
        "bash /tmp/three.sh",
        "```",
        'MCP servers expected: ["khive", "lion"]',
        "Loop file: /x/.khive/loop/x_loop.md",
        "",
        "The person's words, verbatim, oldest first (8):",
        *[line for i in range(1, 9) for line in (f"[10:0{i}]", "```", f"placeholder word {i}", "```")],
        "",
        "Lanes (tasks active):",
        '- {"kind": "task", "id": "t1", "title": "placeholder lane", "awaiting": null}',
        "",
        "Spawned agents the transcript shows running at the take:",
        '- {"id": "a1", "task": "job one", "state": "running-at-take"}',
        "",
        "Topic: placeholder-topic",
        "",
        "Declared:",
        "- branch: lionagi/checkpoint",
        "",
        "The last 3 exchange(s) as they stood; 1 earlier folded, the transcript keeps them: /t/x.jsonl",
        "[2026-09-24T10:00:02Z] in:",
        "```",
        "prompt 2",
        "```",
        "out:",
        "```",
        "text two",
        "```",
        "[2026-09-24T10:00:03Z] wake:",
        "```",
        "prompt 3",
        "```",
        "[2026-09-24T10:00:04Z] in:",
        "```",
        "prompt 4",
        "```",
        "out:",
        "```",
        "text four",
        "```",
        "",
        "Your posture at the take:",
        "quiet now",
        "",
        "Your loop file's posture (loop.posture):",
        "holding",
        "",
        "What to do first: placeholder next",
    ]
    assert render_prompt(c).splitlines() == expected
    assert "placeholder word 0" not in render_prompt(c)  # nine handed, the newest eight carried


def test_a_bare_checkpoint_renders_what_it_lacks_as_such_and_a_lazy_posture_by_its_loader():
    c = take(
        {"transcript": "/t/x.jsonl", "context": {}},
        [var("posture", "posture", "p" * (LAZY + 1), "transcript")],
        actor=ACTOR,
        why="manual",
        budget=8,
        now=AT,
    )
    assert render_prompt(c) == "\n".join(
        [
            f"Checkpoint {ID} of actor:one, taken 2026-09-24T10:05:00+00:00 (manual); context at the take "
            "not known. There is no watch to re-arm.",
            "",
            "Lanes (not read):",
            "- none",
            "",
            "The last 0 exchange(s) as they stood; 0 earlier folded, the transcript keeps them: /t/x.jsonl",
            "",
            "Your posture at the take:",
            f"{LAZY + 1} chars, not shown here; load it with `lion context show --id {ID} --var posture`",
            "",
            "What to do first: re-arm the watches above, then take up the newest of the person's words and "
            "the lanes from where the posture leaves them.",
        ]
    )
    # a figure with only the percentage still reads as one; a view with nothing in it is no exchange
    partial = dataclasses.replace(c, source={**c.source, "context": {"pct": 37}}, view={})
    assert "context at the take ~None of None tokens (37%)." in render_prompt(partial)
    assert "The last 0 exchange(s) as they stood; 0 earlier folded" in render_prompt(partial)


@given(st.text(alphabet="`a \n", max_size=40))
def test_a_directive_reads_back_byte_for_byte_whatever_backticks_it_holds(text):
    c = take({}, [var("d", "directive", text, "hook", "t")], actor=ACTOR, why="manual", budget=1, now=AT)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    assert f"[t]\n{fence}\n{text}\n{fence}\n\nLanes (not read):" in render_prompt(c)


def scripted_git(monkeypatch, *outcomes) -> list:
    """A git that answers each call from `outcomes`: (rc, stdout, stderr), or an exception to raise."""
    calls: list = []

    def run(argv, **kw):
        calls.append(argv)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return subprocess.CompletedProcess(argv, rc, out, err)

    fake = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(module, "subprocess", fake)
    return calls


TRUE = (0, "true\n", "")


@pytest.mark.parametrize(
    ("outcomes", "raises", "said"),
    [
        (
            (OSError("no git here"),),
            CheckpointError,
            "git could not be asked about .*: no git here; nothing was written",
        ),
        ((subprocess.TimeoutExpired("git", 30),), CheckpointError, "git could not be asked about"),
        (((128, "", "fatal: not a git repository (or any of the parent directories): .git\n"),), None, ""),
        (((1, "", ""),), CheckpointError, "git could not say whether .* is in a repository: rc 1; nothing"),
        (((1, "", "fatal: odd\n"),), CheckpointError, "is in a repository: fatal: odd; nothing was written"),
        (((0, "false\n", ""),), None, ""),
        (
            (TRUE, OSError("gone")),
            CheckpointError,
            "git check-ignore could not be asked about .*: gone; nothing",
        ),
        ((TRUE, (0, "", "")), None, ""),
        ((TRUE, (1, "", "")), NotIgnored, "does not ignore it, so a checkpoint could be committed"),
        (
            (TRUE, (128, "", "fatal: bad pattern\n")),
            CheckpointError,
            "could not answer for .*: fatal: bad pattern",
        ),
        ((TRUE, (2, "", "")), CheckpointError, "could not answer for .*: rc 2; nothing was written"),
    ],
)
def test_assert_ignored_reads_gits_two_answers_and_refuses_when_git_cannot_answer(
    tmp_path, monkeypatch, outcomes, raises, said
):
    calls = scripted_git(monkeypatch, *outcomes)
    if raises is None:
        assert assert_ignored(tmp_path) is None
    else:
        with pytest.raises(raises, match=said):
            assert_ignored(tmp_path)
    assert len(calls) == len(outcomes)
    assert calls[0][1:] == ["-C", str(tmp_path), "rev-parse", "--is-inside-work-tree"]
    if len(calls) == 2:
        assert calls[1][1:] == ["-C", str(tmp_path), "check-ignore", "-q", ".khive/context/"]


def test_a_take_refused_by_the_gitignore_writes_nothing_and_an_ignored_one_lands(tmp_path, monkeypatch):
    isolated_git(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    below = repo / "sub" / "x"
    below.mkdir(parents=True)
    c = take({}, [], actor=ACTOR, why="manual", budget=100, now=AT)
    for d in (repo, below):
        with pytest.raises(NotIgnored, match="does not ignore it"):
            save(c, d)
        assert not (d / ".khive").exists()
    # the root form reaches the root only; a directory below needs the `**/` form the refusal names
    (repo / ".gitignore").write_text(".khive/context/\n")
    assert save(c, repo) == repo / ".khive" / "context" / f"{ID}.json"
    with pytest.raises(NotIgnored, match=r"`\*\*/.khive/context/` when it sits below it"):
        save(c, below)
    (repo / ".gitignore").write_text("**/.khive/context/\n")
    assert save(c, below).is_file()
    # the .git directory is in the repository but not its work tree; outside any repository the take proceeds
    assert assert_ignored(repo / ".git") is None
    probe = subprocess.run(["git", "-C", str(tmp_path), "rev-parse"], capture_output=True, text=True)
    assert "not a git repository" in probe.stderr
    (tmp_path / "plain").mkdir()
    assert save(c, tmp_path / "plain").is_file()
    # a directory that does not exist is not "outside a repository": git cannot answer, so the take refuses
    with pytest.raises(CheckpointError, match="git could not say whether .* is in a repository"):
        save(c, tmp_path / "absent")


def test_save_lands_the_file_names_it_latest_and_never_overwrites(plain):
    view = fold_turns([turn(1, "one")], 100)
    c = take(
        {"transcript": "/t.jsonl"},
        [var("a", "fact", ["x", 1])],
        actor=ACTOR,
        why="manual",
        budget=100,
        view=view,
        now=AT,
    )
    ctx = plain / ".khive" / "context"
    first = save(c, plain)
    assert first == ctx / f"{ID}.json" and json.loads((ctx / "latest.json").read_text()) == {"id": ID}
    assert load(plain) == c and load(plain, ID) == c
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
    second, third = save(c, plain), save(c, plain)
    assert [p.stem for p in (second, third)] == [f"{ID}-02", f"{ID}-03"]
    assert json.loads(first.read_text()) == doc and json.loads(third.read_text())["id"] == f"{ID}-03"
    assert load(plain).id == f"{ID}-03" and load(plain, f"{ID}-02").id == f"{ID}-02"
    assert dataclasses.replace(load(plain), id=ID) == c
    assert sorted(p.name for p in ctx.iterdir()) == [
        f"{ID}-02.json",
        f"{ID}-03.json",
        f"{ID}.json",
        "latest.json",
    ]


def test_load_says_what_is_missing_or_malformed_and_takes_only_a_name(plain):
    with pytest.raises(CheckpointError, match="latest.json is absent"):
        load(plain)
    ctx = plain / ".khive" / "context"
    ctx.mkdir(parents=True)
    for latest in ("not json", '{"nope": 1}', "[1]"):
        (ctx / "latest.json").write_text(latest)
        with pytest.raises(CheckpointError, match="latest.json does not name a checkpoint"):
            load(plain)
    (ctx / "latest.json").write_text('{"id": "../../etc/passwd"}')
    with pytest.raises(CheckpointError, match="checkpoint id '../../etc/passwd' is not a name in"):
        load(plain)
    for bad in (7, "", ".hidden", "a/b", f"{ID}\n"):
        with pytest.raises(CheckpointError, match="is not a name in"):
            load(plain, bad)
    with pytest.raises(CheckpointError, match="no checkpoint 'ckpt-absent' in"):
        load(plain, "ckpt-absent")


def test_concurrent_saves_each_land_and_latest_names_one_of_them(plain):
    c = take({}, [], actor=ACTOR, why="manual", budget=100, now=AT)
    written: list[Path] = []
    threads = [threading.Thread(target=lambda: written.append(save(c, plain))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ctx = plain / ".khive" / "context"
    assert sorted(p.name for p in written) == sorted(p.name for p in ctx.glob("ckpt-*.json"))
    assert sorted(p.name for p in written) == sorted(
        [f"{ID}.json", *(f"{ID}-{n:02d}.json" for n in range(2, 13))]
    )
    assert not list(ctx.glob(".latest.json.*")) and (ctx / f"{load(plain).id}.json").exists()


def test_a_take_keeps_its_own_copy_of_what_it_was_handed():
    # a var's value, the view and the source were kept by reference: an edit after the take showed in
    # the checkpoint, and a hook's frame written into a kept input after the take rendered
    watch = {"command": "gh pr view 1", "description": "the pull request", "timeout_ms": 1000}
    source = {"session_id": "s1", "context": {"used": 10, "window": 100}}
    view = fold_turns([turn(1, "one")], 100)
    c = take(source, [var("w", "watch", watch)], actor=ACTOR, why="manual", budget=100, view=view, now=AT)
    watch["command"] = "echo changed"
    source["context"]["used"] = 99
    view["kept"][0]["input"] = "<system-reminder>injected</system-reminder>"
    assert c.vars[0].value["command"] == "gh pr view 1" and c.source["context"]["used"] == 10
    assert c.view["kept"][0]["input"] == "prompt 1"
    prompt = render_prompt(c)
    assert "gh pr view 1" in prompt and "changed" not in prompt and "injected" not in prompt


def test_save_refuses_an_id_that_is_not_a_name_and_writes_nothing(plain):
    # `save` wrote `<id>.json` from whatever the id held: '../escaped' landed outside the context
    # directory and 'latest' took the pointer's own file, while `load` takes only a name
    c = take({}, [], actor=ACTOR, why="manual", budget=100, now=AT)
    ctx = plain / ".khive" / "context"
    for bad in ("../escaped", "latest", "", ".hidden", "a/b", f"{ID}\n"):
        with pytest.raises(CheckpointError, match="is not a name; nothing was written"):
            save(dataclasses.replace(c, id=bad), plain)
    assert not ctx.exists() and not (plain / ".khive" / "escaped.json").exists()
    assert save(c, plain) == ctx / f"{ID}.json" and load(plain).id == ID


def test_a_value_the_file_cannot_carry_is_refused_before_anything_is_written(plain):
    # a lone surrogate passes `json.dumps` escaped and fails at the file: the exclusive create had
    # already landed, so an empty `<id>.json` stayed behind
    first = take({}, [var("ok", "fact", "snowman: ☃")], actor=ACTOR, why="manual", budget=100, now=AT)
    save(first, plain)
    ctx = plain / ".khive" / "context"
    before = sorted(p.name for p in ctx.iterdir()), (ctx / "latest.json").read_text()
    lone = take(
        {}, [var("bad", "fact", "lone \ud800 surrogate")], actor=ACTOR, why="manual", budget=100, now=AT
    )
    with pytest.raises(CheckpointError, match="a value the file cannot carry .*; nothing was written"):
        save(lone, plain)
    assert (sorted(p.name for p in ctx.iterdir()), (ctx / "latest.json").read_text()) == before
    assert load(plain).vars[0].value == "snowman: ☃"
