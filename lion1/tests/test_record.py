import dataclasses
import sys

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from lionagi.record import Directive, Entry, Kind, Record, ViewEntry, directives, fold

# The record's own names, spelled out here so the tests do not borrow the module's table (ADR-0007/C1).
PREFIX = {Kind.INPUT: "_in", Kind.SYSTEM: "_sys", Kind.TEXT: "_m", Kind.RESULT: "_r"}


def make(*kinds: Kind) -> Record:
    r = Record("run-1")
    for i, k in enumerate(kinds):
        r.append(k, f"value {i}")
    return r


def direct(r: Record, op: str, *refs: str, text: str = "", seqs=None, kind: Kind = Kind.RESULT) -> Entry:
    """Append the entry that carries a directive, dated at its own sequence."""
    d = Directive(op, refs, text=text, at=r.seq, seqs=seqs)
    return r.append(kind, f"{op} ok", directive=d)


def shape(view: list[ViewEntry]) -> list[str]:
    out = []
    for v in view:
        s = v.entry.name
        if v.summary is not None:
            s += f" summary={v.summary}"
        if v.hidden:
            s += " hidden"
        out.append(s)
    return out


# the record


def test_there_are_four_kinds():
    assert [k.value for k in Kind] == ["input", "system", "text", "result"]


def test_each_entry_is_named_by_its_kind_and_sequence():
    r = make(Kind.INPUT, Kind.SYSTEM, Kind.TEXT, Kind.RESULT)
    assert [e.name for e in r] == ["_in0", "_sys1", "_m2", "_r3"]
    assert [e.seq for e in r] == [0, 1, 2, 3]
    assert r.run_id == "run-1"


def test_seq_is_the_number_the_next_append_takes():
    r = Record("run-1")
    assert (r.seq, len(r)) == (0, 0)
    first = r.append(Kind.INPUT, "task")
    assert first.seq == 0
    assert (r.seq, len(r)) == (1, 1)
    assert r.append(Kind.TEXT, "reply").seq == 1
    assert r.seq == 2


def test_append_keeps_content_alias_round_and_meta():
    r = Record("run-1")
    plain = r.append(Kind.TEXT, "hello")
    assert (plain.alias, plain.round, plain.meta) == (None, 0, {})
    e = r.append(Kind.RESULT, {"lines": 3}, alias="f", round=2, request="read_file", ok=True)
    assert e.kind is Kind.RESULT
    assert e.content == {"lines": 3}
    assert (e.alias, e.round) == ("f", 2)
    assert e.meta == {"request": "read_file", "ok": True}


def test_an_entry_cannot_be_changed():
    e = make(Kind.TEXT).by_seq(0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.content = "rewritten"
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.seq = 5


def test_entries_is_a_snapshot_not_a_handle_on_the_record():
    r = make(Kind.INPUT, Kind.TEXT)
    snap = r.entries
    assert isinstance(snap, tuple)
    r.append(Kind.RESULT, "later")
    assert [e.name for e in snap] == ["_in0", "_m1"]
    assert [e.name for e in r.entries] == ["_in0", "_m1", "_r2"]


def test_iteration_and_by_seq_follow_record_order():
    r = make(Kind.INPUT, Kind.TEXT, Kind.RESULT)
    assert list(r) == list(r.entries)
    assert [r.by_seq(i).name for i in range(3)] == ["_in0", "_m1", "_r2"]


def test_get_resolves_a_runtime_name_and_an_alias_to_the_same_entry():
    r = make(Kind.INPUT, Kind.TEXT)
    e = r.append(Kind.RESULT, "found", alias="f")
    assert r.get("_r2") is e
    assert r.get("f") is e
    assert r.get("_in0") is r.by_seq(0)
    assert r.get("_m1") is r.by_seq(1)


@pytest.mark.parametrize(
    "name",
    [
        "_r3",  # one past the end
        "_r99",
        "_m0",  # entry 0 is INPUT, not TEXT
        "_in2",  # entry 2 is a RESULT
        "_sys1",
        "_r",
        "_rx",
        "_r-1",
        "_q1",
        "r2",
        "ghost",
        "",
    ],
)
def test_get_returns_none_for_a_name_nothing_is_called(name):
    r = make(Kind.INPUT, Kind.TEXT)
    r.append(Kind.RESULT, "found", alias="f")
    assert r.get(name) is None


def test_names_and_aliases_keep_pointing_at_the_same_entry_as_the_record_grows():
    r = make(Kind.INPUT)
    e = r.append(Kind.RESULT, "found", alias="f")
    for i in range(40):
        r.append(Kind.RESULT, i, alias=f"x{i}")
    assert r.get("_r1") is e
    assert r.get("f") is e
    assert r.get("x39") is r.by_seq(41)


@pytest.mark.parametrize("alias", ["_x", "_r0", "_", "__f"])
def test_an_alias_starting_with_underscore_is_refused_and_nothing_is_appended(alias):
    r = make(Kind.RESULT)
    with pytest.raises(ValueError, match=f"alias '{alias}' starts with _"):
        r.append(Kind.RESULT, "v", alias=alias)
    assert len(r) == 1
    assert not r.has_alias(alias)


def test_an_alias_is_registered_once():
    r = make(Kind.INPUT)
    first = r.append(Kind.RESULT, "one", alias="f")
    with pytest.raises(ValueError, match="alias 'f' already names _r1"):
        r.append(Kind.RESULT, "two", alias="f")
    assert len(r) == 2
    assert r.get("f") is first


class _LooksPlain(str):
    # Answers the underscore check as if the name had none. Every runtime name starts with `_`, so a
    # plain str is refused by that check first and never reaches the guard this exercises.
    def startswith(self, prefix, *args):
        return prefix != "_" and super().startswith(prefix, *args)


def test_an_alias_that_is_a_runtime_name_in_use_is_refused():
    r = make(Kind.RESULT)
    with pytest.raises(ValueError, match="alias '_r0' is the name of an entry on the record"):
        r.append(Kind.TEXT, "v", alias=_LooksPlain("_r0"))
    assert len(r) == 1


def test_has_alias_knows_only_the_models_names():
    r = make(Kind.INPUT)
    r.append(Kind.RESULT, "found", alias="f")
    assert r.has_alias("f")
    assert not r.has_alias("_r1")
    assert not r.has_alias("g")


def test_latest_is_the_newest_entry_of_a_kind():
    r = make(Kind.INPUT, Kind.RESULT, Kind.TEXT, Kind.RESULT, Kind.SYSTEM)
    assert r.latest(Kind.RESULT) is r.by_seq(3)
    assert r.latest(Kind.INPUT) is r.by_seq(0)
    assert make(Kind.INPUT, Kind.TEXT).latest(Kind.RESULT) is None
    assert Record("empty").latest(Kind.TEXT) is None


def test_of_kind_lists_a_kinds_entries_in_record_order():
    r = make(Kind.RESULT, Kind.INPUT, Kind.RESULT, Kind.TEXT, Kind.RESULT)
    assert [e.seq for e in r.of_kind(Kind.RESULT)] == [0, 2, 4]
    assert r.of_kind(Kind.SYSTEM) == []


def test_directives_are_read_off_result_and_system_entries_in_record_order():
    r = Record("run-1")
    first = Directive("hide", ("a",), at=0)
    ignored_text = Directive("hide", ("b",), at=1)
    fold_event = Directive("summarize", ("c",), text="s", at=2)
    ignored_input = Directive("show", ("d",), at=3)
    last = Directive("show", ("e",), at=6)
    r.append(Kind.RESULT, "ok", directive=first)
    r.append(Kind.TEXT, "prose", directive=ignored_text)
    r.append(Kind.SYSTEM, "notice", directive=fold_event)
    r.append(Kind.INPUT, "job", directive=ignored_input)
    r.append(Kind.RESULT, "ok", directive={"op": "hide", "refs": ["a"]})
    r.append(Kind.RESULT, "ok")
    r.append(Kind.RESULT, "ok", directive=last)
    assert directives(r) == [first, fold_event, last]


def test_a_directive_is_frozen():
    d = Directive("hide", ("f",), at=3)
    assert (d.text, d.seqs) == ("", None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.op = "show"


# the view


def test_with_no_directives_the_view_is_every_entry_as_itself_in_order():
    assert fold(Record("empty")) == []
    r = make(Kind.INPUT, Kind.SYSTEM, Kind.TEXT, Kind.RESULT)
    view = fold(r)
    assert view == [ViewEntry(entry=e) for e in r]
    assert all(v.entry is e for v, e in zip(view, r, strict=True))


def test_hide_leaves_a_placeholder_where_the_entry_was_and_the_record_is_untouched():
    r = make(Kind.INPUT, Kind.TEXT)
    found = r.append(Kind.RESULT, "a large value", alias="f")
    direct(r, "hide", "f")
    before = r.entries
    assert shape(fold(r)) == ["_in0", "_m1", "_r2 hidden", "_r3"]
    assert fold(r)[2] == ViewEntry(entry=found, hidden=True)
    assert r.entries == before
    assert r.get("f").content == "a large value"


def test_show_brings_a_hidden_entry_back_as_itself():
    r = make(Kind.INPUT, Kind.TEXT, Kind.RESULT)
    direct(r, "hide", "_m1", "_r2")
    direct(r, "show", "_r2")
    assert shape(fold(r)) == ["_in0", "_m1 hidden", "_r2", "_r3", "_r4"]


def test_summarize_puts_its_text_in_front_of_where_the_span_began_and_drops_the_span():
    r = make(Kind.INPUT, Kind.TEXT, Kind.RESULT, Kind.RESULT)
    direct(r, "summarize", "_r3", "_m1", text="read two files")
    assert shape(fold(r)) == ["_in0", "_m1 summary=read two files", "_r2", "_r4"]
    assert fold(r)[1] == ViewEntry(entry=r.by_seq(1), summary="read two files")


def test_two_summaries_starting_at_one_entry_render_in_record_order():
    r = make(Kind.TEXT, Kind.RESULT, Kind.RESULT)
    direct(r, "summarize", "_m0", "_r1", text="first")
    direct(r, "summarize", "_m0", "_r2", text="second")
    assert shape(fold(r)) == ["_m0 summary=first", "_m0 summary=second", "_r3", "_r4"]


def test_show_after_summarize_brings_the_span_back_without_its_summary():
    r = make(Kind.TEXT, Kind.RESULT, Kind.RESULT)
    direct(r, "summarize", "_m0", "_r1", text="gone")
    direct(r, "show", "_m0", "_r1")
    assert shape(fold(r)) == ["_m0", "_r1", "_r2", "_r3", "_r4"]


def test_an_entry_shown_out_of_a_summary_and_hidden_again_is_a_placeholder():
    r = make(Kind.RESULT, Kind.RESULT)
    direct(r, "summarize", "_r0", "_r1", text="both")
    direct(r, "show", "_r0", "_r1")
    direct(r, "hide", "_r0")
    assert shape(fold(r)) == ["_r0 hidden", "_r1", "_r2", "_r3", "_r4"]


def test_a_directive_reaches_only_entries_before_itself():
    r = make(Kind.RESULT)
    direct(r, "hide", "_r0", "_r1", "_r2")
    r.append(Kind.RESULT, "after")
    assert shape(fold(r)) == ["_r0 hidden", "_r1", "_r2"]


def test_a_pinned_directive_reaches_only_entries_before_itself():
    r = make(Kind.RESULT)
    direct(r, "hide", seqs=(0, 1, 2))
    r.append(Kind.RESULT, "after")
    assert shape(fold(r)) == ["_r0 hidden", "_r1", "_r2"]


def test_input_is_never_hidden_or_summarized_while_protected():
    r = make(Kind.INPUT, Kind.TEXT)
    direct(r, "hide", "_in0", "_m1")
    direct(r, "summarize", "_in0", text="the job")
    assert shape(fold(r)) == ["_in0", "_m1 hidden", "_r2", "_r3"]
    assert shape(fold(r, protect_input=True)) == shape(fold(r))
    assert shape(fold(r, protect_input=False)) == ["_in0 summary=the job", "_m1 hidden", "_r2", "_r3"]


def test_pinned_seqs_are_what_the_fold_reads_never_the_names():
    r = make(Kind.RESULT, Kind.RESULT)
    direct(r, "hide", "_r1", seqs=(0,))
    assert shape(fold(r)) == ["_r0 hidden", "_r1", "_r2"]


def test_an_empty_pin_reaches_nothing_even_when_the_names_resolve():
    r = make(Kind.RESULT, Kind.RESULT)
    direct(r, "hide", "_r0", "_r1", seqs=())
    direct(r, "summarize", "_r0", text="nothing", seqs=())
    assert shape(fold(r)) == ["_r0", "_r1", "_r2", "_r3"]


def test_a_name_that_resolves_to_nothing_is_skipped():
    r = make(Kind.INPUT, Kind.TEXT)
    direct(r, "hide", "ghost", "_r99", "_m0", "_m1")
    direct(r, "summarize", "ghost", "_r99", text="nothing")
    assert shape(fold(r)) == ["_in0", "_m1 hidden", "_r2", "_r3"]


def test_an_unknown_op_changes_nothing():
    r = make(Kind.TEXT, Kind.RESULT)
    direct(r, "delete", "_m0", "_r1")
    assert shape(fold(r)) == ["_m0", "_r1", "_r2"]


def test_a_system_entry_carries_the_runtimes_own_hide():
    r = make(Kind.INPUT, Kind.RESULT, Kind.RESULT)
    direct(r, "hide", seqs=(1,), kind=Kind.SYSTEM)
    assert shape(fold(r)) == ["_in0", "_r1 hidden", "_r2", "_sys3"]


def test_a_directive_on_a_text_or_input_entry_is_not_applied():
    r = make(Kind.RESULT, Kind.RESULT)
    direct(r, "hide", "_r0", kind=Kind.TEXT)
    direct(r, "hide", "_r1", kind=Kind.INPUT)
    assert shape(fold(r)) == ["_r0", "_r1", "_m2", "_in3"]


@pytest.mark.xfail(
    strict=True, reason="known defect: showing a span's first entry drops the rest of the span"
)
def test_showing_the_first_entry_of_a_summarized_span_keeps_the_rest_of_the_span_in_the_view():
    r = make(Kind.INPUT, Kind.TEXT, Kind.TEXT, Kind.TEXT)
    direct(r, "summarize", "_m1", "_m2", "_m3", text="three notes")
    direct(r, "show", "_m1")
    view = fold(r)
    assert shape(view).count("_m1") == 1
    # _m2 and _m3 are still summarized, so the summary still stands somewhere in the view (ADR-0007/C3)
    assert [v.summary for v in view if v.summary is not None] == ["three notes"]


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="known defect: a name whose digits int() refuses crashes it"
)
@pytest.mark.parametrize(
    "name",
    ["_r\u00b2", "_r" + "1" * (sys.get_int_max_str_digits() + 1)],
    ids=["superscript-digit", "past-the-int-digit-limit"],
)
def test_a_name_whose_digits_int_refuses_resolves_to_nothing_and_cannot_crash_the_fold(name):
    r = make(Kind.INPUT, Kind.RESULT)
    direct(r, "hide", name)
    assert r.get(name) is None
    assert shape(fold(r)) == ["_in0", "_r1", "_r2"]


# the view, as properties over generated records and directives

OPS = ("hide", "show", "summarize")


@st.composite
def plans(draw):
    """A record's blueprint, one (kind, alias, directive) per entry, and every name that resolves.

    A directive rides a RESULT or SYSTEM entry, dated at that entry's sequence, and either names its
    targets (runtime names, aliases, and names that resolve to nothing) or pins their sequences.
    """
    kinds = draw(st.lists(st.sampled_from(list(Kind)), max_size=12))
    aliased = draw(st.lists(st.booleans(), min_size=len(kinds), max_size=len(kinds)))
    names = {}
    for i, k in enumerate(kinds):
        names[f"{PREFIX[k]}{i}"] = i
        if aliased[i]:
            names[f"a{i}"] = i
    wrong_kind = [f"{PREFIX[Kind.RESULT if k is Kind.TEXT else Kind.TEXT]}{i}" for i, k in enumerate(kinds)]
    pool = sorted(names) + wrong_kind + ["ghost", f"_r{len(kinds)}"]
    plan = []
    for i, k in enumerate(kinds):
        d = None
        if k in (Kind.RESULT, Kind.SYSTEM) and draw(st.booleans()):
            refs = tuple(draw(st.lists(st.sampled_from(pool), max_size=4)))
            seqs = None
            if draw(st.booleans()):
                seqs = tuple(draw(st.lists(st.integers(0, len(kinds) - 1), max_size=4)))
            d = Directive(draw(st.sampled_from(OPS)), refs, text=f"summary {i}", at=i, seqs=seqs)
        plan.append((k, f"a{i}" if aliased[i] else None, d))
    return plan, names


def build(plan) -> Record:
    r = Record("generated")
    for i, (kind, alias, d) in enumerate(plan):
        meta = {} if d is None else {"directive": d}
        r.append(kind, f"value {i}", alias=alias, **meta)
    return r


def effects(plan, names, protect_input):
    """Each directive with the targets the fold should give it, in record order."""
    out = []
    for at, (_, _, d) in enumerate(plan):
        if d is None:
            continue
        raw = d.seqs if d.seqs is not None else [names[ref] for ref in d.refs if ref in names]
        targets = [s for s in raw if s < at and not (protect_input and plan[s][0] is Kind.INPUT)]
        out.append((d, targets))
    return out


def states(plan, names, protect_input):
    """Per entry, (hidden, under a summary), read off the directives after the last show that reached it."""
    fx = effects(plan, names, protect_input)
    out = []
    for s in range(len(plan)):
        shows = [i for i, (d, t) in enumerate(fx) if d.op == "show" and s in t]
        after = fx[shows[-1] + 1 :] if shows else fx
        hidden = any(d.op in ("hide", "summarize") and s in t for d, t in after)
        under = any(d.op == "summarize" and s in t for d, t in after)
        out.append((hidden, under))
    return out


def standing_summaries(plan, names, protect_input):
    """Summary texts by the first entry of their span, for spans whose first entry no later show reached."""
    fx = effects(plan, names, protect_input)
    out = {}
    for i, (d, t) in enumerate(fx):
        if d.op != "summarize" or not t:
            continue
        first = min(t)
        if not any(later.op == "show" and first in lt for later, lt in fx[i + 1 :]):
            out.setdefault(first, []).append(d.text)
    return out


def orphaned(plan, names, protect_input) -> bool:
    """Whether an entry sits hidden under a summary whose span start a later show reached: the case
    test_showing_the_first_entry_of_a_summarized_span_keeps_the_rest_of_the_span_in_the_view records."""
    fx = effects(plan, names, protect_input)
    standing = [
        (i, set(t))
        for i, (d, t) in enumerate(fx)
        if d.op == "summarize" and t and not any(o.op == "show" and min(t) in ot for o, ot in fx[i + 1 :])
    ]
    for s, (hidden, under) in enumerate(states(plan, names, protect_input)):
        if hidden and under:
            shows = [i for i, (d, t) in enumerate(fx) if d.op == "show" and s in t]
            last = shows[-1] if shows else -1
            if not any(i > last and s in t for i, t in standing):
                return True
    return False


def rendered(view, seq):
    return [(v.summary, v.hidden) for v in view if v.entry.seq == seq]


@given(plans(), st.booleans())
def test_fold_never_raises_and_never_changes_the_record(generated, protect_input):
    plan, _ = generated
    r = build(plan)
    before = r.entries
    view = fold(r, protect_input=protect_input)
    assert all(isinstance(v, ViewEntry) for v in view)
    assert all(v.entry is r.by_seq(v.entry.seq) for v in view)
    assert not any(v.hidden and v.summary is not None for v in view)
    assert len(r.entries) == len(before)
    assert all(a is b for a, b in zip(r.entries, before, strict=True))


@given(plans())
def test_with_input_protected_no_input_entry_is_hidden_or_summarized(generated):
    plan, _ = generated
    r = build(plan)
    view = fold(r)
    for e in r.of_kind(Kind.INPUT):
        assert [v for v in view if v.entry is e] == [ViewEntry(entry=e)]


@given(plans(), st.booleans())
def test_the_view_keeps_record_order(generated, protect_input):
    plan, _ = generated
    view = fold(build(plan), protect_input=protect_input)
    seqs = [v.entry.seq for v in view]
    assert seqs == sorted(seqs)
    for s in set(seqs):
        own = rendered(view, s)
        summaries = [x for x in own if x[0] is not None]
        assert own[: len(summaries)] == summaries  # a summary stands in front of the entry it replaced
        assert len(own) - len(summaries) <= 1


@given(plans(), st.booleans())
def test_a_directive_never_affects_its_own_entry_or_any_later_one(generated, protect_input):
    plan, _ = generated
    view = fold(build(plan), protect_input=protect_input)
    for at, (_, _, d) in enumerate(plan):
        if d is None:
            continue
        without = [(k, a, None if i == at else x) for i, (k, a, x) in enumerate(plan)]
        other = fold(build(without), protect_input=protect_input)
        for s in range(at, len(plan)):
            assert rendered(view, s) == rendered(other, s)


@given(plans(), st.booleans())
def test_every_entry_appears_as_itself_exactly_once_unless_hidden(generated, protect_input):
    plan, names = generated
    view = fold(build(plan), protect_input=protect_input)
    for s, (hidden, _) in enumerate(states(plan, names, protect_input)):
        assert rendered(view, s).count((None, False)) == (0 if hidden else 1)


@given(plans(), st.booleans())
def test_a_hidden_entry_not_under_a_summary_appears_once_as_a_placeholder(generated, protect_input):
    plan, names = generated
    assume(not orphaned(plan, names, protect_input))
    view = fold(build(plan), protect_input=protect_input)
    for s, (hidden, under) in enumerate(states(plan, names, protect_input)):
        assert rendered(view, s).count((None, True)) == (1 if hidden and not under else 0)


@given(plans(), st.booleans())
def test_a_summarized_span_puts_its_summary_in_front_of_its_first_entry(generated, protect_input):
    plan, names = generated
    assume(not orphaned(plan, names, protect_input))
    view = fold(build(plan), protect_input=protect_input)
    got = {}
    for v in view:
        if v.summary is not None:
            got.setdefault(v.entry.seq, []).append(v.summary)
    assert got == standing_summaries(plan, names, protect_input)
