"""The checkpoint (ADR-0019): named state plus a folded view, computed with no model call from what the
store holds, never written as a file. The take is pure and checks the kinds and whys it is handed. A
run of this runtime takes none: its rows are the store's (`tests/test_store.py`). The Claude Code
session's take and restore are in `tests/test_claude_code_checkpoint.py`."""

import dataclasses
import json
import types
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError, computed_field, model_serializer

from lion1.actor import Result
from lion1.checkpoint import KINDS, LAZY, WHYS, CheckpointError, Var, entry_of, jsonable, listed, take
from lion1.record import Directive, Entry, Kind

ACTOR = "actor:one"
AT = datetime(2026, 9, 24, 10, 5, tzinfo=UTC)
SOURCE = {"session_id": "s1"}
ID = "s1@0"  # the session the store holds, at the turn it was read to: none yet
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


def test_the_lists_the_adr_names_and_the_two_ceilings():
    assert KINDS == ("fact", "posture")
    assert WHYS == ("threshold", "end", "manual")
    assert LAZY == 400
    c = take(SOURCE, [var("a", "fact", 1)], actor=ACTOR, why="manual", budget=1, now=AT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.why = "end"
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.vars[0].value = 2


def test_a_take_names_itself_keeps_var_order_and_lets_a_later_var_of_a_name_stand():
    given = [var("a", "fact", "1"), var("b", "fact", "2"), var("a", "fact", "3", "hook", "10:04")]
    c = take({**SOURCE, "cwd": "/w"}, given, actor=ACTOR, why="manual", budget=100, now=AT)
    assert c.id == ID and c.taken_at == "2026-09-24T10:05:00+00:00" and c.why == "manual"
    assert c.source == {"session_id": "s1", "cwd": "/w"}  # the source as given: no kind is guessed
    assert [(v.name, v.value, v.source, v.at) for v in c.vars] == [
        ("a", "3", "hook", "10:04"),
        ("b", "2", "declared", ""),
    ]
    assert c.view == {"budget": 100, "kept": [], "hidden": [], "summaries": []}
    # the id: the session and the turn count the source names, else the turns the view holds
    assert take({"session_id": "s2", "turns": 7}, [], actor=ACTOR, why="end", budget=1, now=AT).id == "s2@7"
    view = {"budget": 1, "kept": [turn(3)], "hidden": ["u1@t", "u2@t"], "summaries": []}
    assert take({"session_id": "s3"}, [], actor=ACTOR, why="end", budget=1, view=view, now=AT).id == "s3@3"
    # no clock given: now, with a zone
    fresh = take(SOURCE, [], actor=ACTOR, why="manual", budget=1)
    assert fresh.id == ID and datetime.fromisoformat(fresh.taken_at).tzinfo is not None


def test_a_take_refuses_a_why_or_a_kind_outside_its_lists_and_a_nameless_actor():
    with pytest.raises(CheckpointError, match="why 'later': one of threshold, end, manual"):
        take(SOURCE, [], actor=ACTOR, why="later", budget=1)
    with pytest.raises(CheckpointError, match="no actor: a checkpoint is the actor's"):
        take(SOURCE, [], actor="", why="manual", budget=1)
    with pytest.raises(CheckpointError, match="var 'v': kind 'blob' is not one of fact, posture"):
        take(SOURCE, [var("v", "blob", {"id": "a1"})], actor=ACTOR, why="manual", budget=1)
    # the taker's own lists: a kind and a why off the defaults stand when named, and a kind named eager
    # is never made lazy; a hook's frame is the harness's to refuse, never the run's
    own = take(
        SOURCE,
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
            SOURCE,
            [var("f", "fact", 1)],
            actor=ACTOR,
            why="rotate",
            budget=1,
            kinds=("watch", "directive"),
            whys=("rotate",),
        )
    long = take(
        SOURCE,
        [var("w", "watch", "x" * (LAZY + 1))],
        actor=ACTOR,
        why="w",
        budget=1,
        kinds=["watch"],
        whys=["w"],
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
    c = take(SOURCE, given, actor=ACTOR, why="manual", budget=100, now=AT)
    by = {v.name: v for v in c.vars}
    # a ref names the store that keeps the value; without one the checkpoint carries it
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


def test_listed_refuses_a_why_or_a_kind_off_the_lists_and_names_the_value():
    c = take(
        SOURCE,
        [var("a", "fact", "one"), var("t", "posture", "two")],
        actor=ACTOR,
        why="manual",
        budget=100,
        now=AT,
    )
    whys, kinds = ", ".join(WHYS), ", ".join(KINDS)
    blob = var("v", "blob", {"id": "a1"})
    assert listed(c, "then") is c
    with pytest.raises(CheckpointError, match=f"{ID}: why 'later' is not one of {whys}; then"):
        listed(dataclasses.replace(c, why="later"), "then")
    with pytest.raises(CheckpointError, match=f"{ID}: var 'v' has kind 'blob', not one of {kinds}; then"):
        listed(dataclasses.replace(c, vars=(*c.vars, blob)), "then")
    # the taker's own lists: a checkpoint of another vocabulary stands under it, and only under it
    other = dataclasses.replace(c, why="later", vars=(*c.vars, blob))
    mine = {"kinds": (*KINDS, "blob"), "whys": (*WHYS, "later")}
    assert listed(other, "then", **mine) is other
    with pytest.raises(CheckpointError, match=f"var 'v' has kind 'blob', not one of {kinds}; then"):
        listed(dataclasses.replace(other, why="manual"), "then")


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


class Book(BaseModel):
    title: str
    year: int


class Loose(BaseModel):
    v: object


def test_a_model_value_is_written_as_its_fields_and_one_json_cannot_hold_as_its_str():
    # a var's value that is a model was written, shown and sized as its str, which a restore cannot
    # read a field of
    book = Book(title="Dune", year=1965)
    odd = Loose(v=types.SimpleNamespace(x=1))  # pydantic has no JSON form for the namespace
    assert jsonable(book) == {"title": "Dune", "year": 1965}
    assert jsonable(odd) == str(odd) == "v=namespace(x=1)" and jsonable(AT) == str(AT)
    c = take(SOURCE, [var("value.b", "fact", book)], actor=ACTOR, why="manual", budget=100, now=AT)
    assert json.loads(json.dumps(c.vars[0].value, default=jsonable)) == {"title": "Dune", "year": 1965}


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


def test_a_model_whose_dump_is_not_an_object_is_written_as_that_dump():
    # a root model dumps its root and a serializer may return a scalar: the row holds that JSON, and a
    # restore reads it back as it was written
    tags, mark = Tags(["Dune", "sf"]), Mark(title="Dune")
    assert (jsonable(tags), jsonable(mark)) == (["Dune", "sf"], "Dune")
    c = take(
        SOURCE,
        [var("value.t", "fact", tags), var("value.m", "fact", mark)],
        actor=ACTOR,
        why="manual",
        budget=100,
        now=AT,
    )
    written = json.loads(json.dumps({v.name: v.value for v in c.vars}, default=jsonable))
    assert written == {"value.t": ["Dune", "sf"], "value.m": "Dune"}
