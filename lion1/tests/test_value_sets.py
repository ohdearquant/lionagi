"""Every field of the core whose values form a closed set carries the set in its type, and refuses
any other value when it is made, the error naming the set. A field whose values are another party's
to name stays a string: an event's kind (any handler may emit its own) and a checkpoint var's kind and
why (each taker passes its own lists)."""

import asyncio
import json
import typing

import pytest

from lion1.actor import (
    Effect,
    Exhausted,
    Handler,
    Language,
    Message,
    Profile,
    ProfileError,
    Reason,
    Result,
    Role,
    Run,
    Spent,
)
from lion1.bus import Event
from lion1.checkpoint import Var, entry_of, entry_row, load_log
from lion1.record import Directive, Entry, Kind, Op
from lion1.store import CallStatus, Store
from tests.lionfake import store

from .test_actor import Echo


async def _echo(req, ctx):
    return req.text


# (the set's alias, what the error calls it, the values the set must hold, a maker taking one value)
SETS = [
    (Role, "message role", ("system", "user", "assistant"), lambda v: Message(v, "hi")),
    (Language, "profile language", ("core", "program"), lambda v: Profile("p", language=v)),
    (Effect, "handler effect", ("write", "read", "pure"), lambda v: Handler(Echo, _echo, effect=v)),
    (
        Reason,
        "result reason",
        ("unknown", "invalid", "privilege", "refused", "failed", "cancelled"),
        lambda v: Result("echo", False, error="no", reason=v),
    ),
    (Spent, "exhausted reason", ("turns", "idle", "time", "cost", "context"), lambda v: Exhausted(3, v)),
    (Op, "directive op", ("hide", "show", "summarize"), lambda v: Directive(v, ("_m1",))),
]


@pytest.mark.parametrize("alias, what, values, make", SETS, ids=[s[1] for s in SETS])
def test_a_closed_value_set_is_the_fields_type_and_any_other_value_is_refused_naming_it(
    alias, what, values, make
):
    assert typing.get_args(alias) == values
    for value in values:
        make(value)
    # None is a value too, but where the field may be unset (a result's reason) it is not a wrong one
    wrongs = ("", "nope", values[0].upper(), f" {values[0]}", 0, *(() if alias is Reason else (None,)))
    for wrong in wrongs:
        with pytest.raises((ValueError, ProfileError)) as refused:
            make(wrong)
        assert str(refused.value) == f"{what} {wrong!r} is not one of {', '.join(values)}"


@pytest.mark.parametrize(
    "cls, field, alias",
    [
        (Message, "role", Role),
        (Profile, "language", Language),
        (Handler, "effect", Effect),
        (Result, "reason", Reason | None),
        (Exhausted, "reason", Spent),
        (Directive, "op", Op),
        (Entry, "kind", Kind),
    ],
)
def test_each_field_is_annotated_with_its_set(cls, field, alias):
    assert typing.get_type_hints(cls)[field] == alias


def test_a_call_closes_with_a_status_of_its_set():
    assert typing.get_type_hints(Store.call_close, localns={"Run": Run})["status"] == CallStatus


def test_a_stored_result_whose_reason_the_set_lacks_reads_back_with_none_and_its_error(tmp_path):
    # a hook could give a result any reason before the set was closed, and such a row is in the store
    row = entry_row(Entry(4, Kind.RESULT, Result("echo", False, error="took too long", reason="failed")))
    row["content"]["reason"] = "timeout"
    back = entry_of(json.loads(json.dumps(row)))
    assert (back.content.reason, back.content.error, back.content.ok) == (None, "took too long", False)
    log = tmp_path / "old.jsonl"
    log.write_text(json.dumps(row) + "\n")
    assert [e.content.reason for e in load_log(log)] == [None]
    kept = entry_row(Entry(4, Kind.RESULT, Result("echo", False, error="no", reason="refused")))
    assert entry_of(json.loads(json.dumps(kept))).content.reason == "refused"  # one of the set stands


def test_a_result_with_no_reason_is_one_that_succeeded_or_was_never_given_one():
    assert Result("echo", True, value="hi").reason is None
    assert Result("echo", False, error="no").reason is None


def test_an_entrys_kind_is_a_member_of_kind_and_its_bare_value_is_refused():
    members = "Kind.INPUT, Kind.SYSTEM, Kind.TEXT, Kind.RESULT"
    assert [k.value for k in Kind] == ["input", "system", "text", "result"]
    assert [Entry(0, k, "x").name for k in Kind] == ["_in0", "_sys0", "_m0", "_r0"]
    for wrong in ("text", "TEXT", None, 0):
        with pytest.raises(ValueError) as refused:
            Entry(0, wrong, "x")
        assert str(refused.value) == f"entry kind {wrong!r} is not one of {members}"


def test_a_call_status_outside_its_set_is_refused_before_anything_is_written():
    s = store()
    assert typing.get_args(CallStatus) == ("ok", "refused", "cut", "failed")
    before = len(s.khive.lion.ops)
    with pytest.raises(ValueError, match="call status 'done' is not one of ok, refused, cut, failed"):
        asyncio.run(s.call_close(None, "c1", status="done"))
    assert len(s.khive.lion.ops) == before


def test_an_events_kind_and_a_checkpoint_vars_kind_stay_open_strings():
    assert typing.get_type_hints(Event)["kind"] is str
    assert Event("my.own.event").kind == "my.own.event"
    hints = typing.get_type_hints(Var)
    assert hints["kind"] is str and hints["source"] is str
