# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pure checkpoint codec and immutable capture contracts."""

from __future__ import annotations

import json

import pytest

from lionagi import _checkpoint_v3_codec as codec
from lionagi._checkpoint_v3_codec import (
    CodecError,
    FrozenArray,
    FrozenObject,
    JournalRecord,
    PatchOp,
    apply_patch,
    capture_context,
    decode_json,
    decode_record,
    diff_context,
    encode_record,
    freeze_json,
    thaw_json,
    validate_entry,
)

GENERATION = "0123456789abcdef0123456789abcdef"


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("count", [0, 1, 31, 32, 33, 1024, 1025])
def test_single_nested_append_is_bounded_and_shares_unchanged_nodes(count):
    live = {"nested": {"items": list(range(count))}, "other": {"keep": [1, 2]}}
    before, _ = capture_context(live)
    live["nested"]["items"].append("x")
    after, patch = capture_context(live, before)
    assert patch == (PatchOp("add", "/nested/items/-", "x"),)
    assert before["other"] is after["other"]
    old_array, new_array = before["nested"]["items"], after["nested"]["items"]
    if count == 1024:
        assert old_array._root is new_array._root.children[0]
    elif count >= 64:
        assert old_array._root.children[0] is new_array._root.children[0]
    assert thaw_json(before)["nested"]["items"] == list(range(count))
    assert thaw_json(apply_patch(before, patch)) == live

    entry = {"agent_id": "op", "status": "completed", "response": "ok"}
    encoded = encode_record(GENERATION, 1, "op", freeze_json(entry), patch)
    e = len(compact(entry))
    u = len(compact("x"))
    p = len(compact("/nested/items/-"))
    assert len(encoded) <= e + u + p + 256
    assert json.loads(encoded)["flow_context"]["patch"] == [
        {"op": "add", "path": "/nested/items/-", "value": "x"}
    ]
    assert decode_record(encoded).patch == patch


def test_tail_shrink_replays_without_rebuilding_arrays(monkeypatch):
    before = freeze_json({"items": list(range(1000))})
    target = freeze_json({"items": list(range(10))})
    patch = diff_context(before, target)

    def forbid_rebuild(_values):
        raise AssertionError("tail removal should not rebuild the array")

    monkeypatch.setattr(codec, "_array_from_values", forbid_rebuild)
    replayed = apply_patch(before, patch)
    assert len(patch) == 990
    assert replayed == target
    assert thaw_json(replayed) == {"items": list(range(10))}
    assert len(before["items"]) == 1000


@pytest.mark.parametrize("count", [1_000, 10_000, 100_000])
@pytest.mark.parametrize("position", ["front", "middle"])
def test_single_array_insertion_has_one_bounded_record(count, position):
    values = list(range(count))
    before = freeze_json({"nested": {"items": values}})
    index = 0 if position == "front" else count // 2
    values.insert(index, "inserted")
    target = freeze_json({"nested": {"items": values}})
    patch = diff_context(before, target)
    path = f"/nested/items/{index}"
    assert patch == (PatchOp("add", path, "inserted"),)
    entry = {"agent_id": "op", "status": "completed", "response": "ok"}
    line = encode_record(GENERATION, 1, "op", freeze_json(entry), patch)
    assert len(line) <= len(compact(entry)) + len(compact("inserted")) + len(compact(path)) + 256
    assert thaw_json(apply_patch(before, patch)) == {"nested": {"items": values}}


@pytest.mark.parametrize("position", ["front", "middle"])
def test_single_array_deletion_replays_as_one_remove(position):
    values = list(range(1_000))
    before = freeze_json({"items": values})
    index = 0 if position == "front" else len(values) // 2
    values.pop(index)
    target = freeze_json({"items": values})
    patch = diff_context(before, target)
    assert patch == (PatchOp("remove", f"/items/{index}"),)
    assert thaw_json(apply_patch(before, patch)) == {"items": values}


def test_duplicate_array_insert_replays_exactly():
    before = freeze_json({"items": ["x", "x", "x"]})
    target = freeze_json({"items": ["x", "x", "x", "x"]})
    patch = diff_context(before, target)
    assert len(patch) == 1 and patch[0].op == "add"
    assert thaw_json(apply_patch(before, patch)) == thaw_json(target)


@pytest.mark.parametrize(
    "old, new, types", [([1], [True, 1], [bool, int]), ([1.0], [1, 1.0], [int, float])]
)
def test_array_insertion_preserves_distinct_scalar_types(old, new, types):
    assert not codec._same_scalar(old[0], new[0])
    before, target = freeze_json({"items": old}), freeze_json({"items": new})
    patch = diff_context(before, target)
    assert len(patch) == 1 and patch[0].op == "add"
    recovered = thaw_json(apply_patch(before, patch))["items"]
    assert recovered == new
    assert [type(value) for value in recovered] == types


def test_array_insertion_compares_equal_but_distinct_frozen_elements():
    before = freeze_json({"items": [{"value": [1]}, {"value": [2]}]})
    values = thaw_json(before)["items"]
    values.insert(1, {"new": True})
    target = freeze_json({"items": values})
    assert before["items"][0] is not target["items"][0]
    assert before["items"][1] is not target["items"][2]
    patch = diff_context(before, target)
    assert patch == (PatchOp("add", "/items/1", freeze_json({"new": True})),)
    assert thaw_json(apply_patch(before, patch)) == thaw_json(target)


def test_array_multiple_insertions_and_deletions_have_valid_order():
    before = freeze_json({"items": [0, 3]})
    expanded = freeze_json({"items": [0, 1, 2, 3]})
    additions = diff_context(before, expanded)
    assert additions == (PatchOp("add", "/items/1", 1), PatchOp("add", "/items/2", 2))
    assert thaw_json(apply_patch(before, additions)) == thaw_json(expanded)
    removals = diff_context(expanded, before)
    assert removals == (PatchOp("remove", "/items/2"), PatchOp("remove", "/items/1"))
    assert thaw_json(apply_patch(expanded, removals)) == thaw_json(before)


def test_array_multi_site_edits_keep_previous_per_index_diff():
    before = freeze_json({"items": [1, 2, 3, 4]})
    target = freeze_json({"items": [0, 1, 2, 4, 5]})
    patch = diff_context(before, target)
    assert patch == (
        PatchOp("replace", "/items/0", 0),
        PatchOp("replace", "/items/1", 1),
        PatchOp("replace", "/items/2", 2),
        PatchOp("add", "/items/-", 5),
    )
    assert thaw_json(apply_patch(before, patch)) == thaw_json(target)


@pytest.mark.parametrize(
    "old, new, operations",
    [
        ([1, 2], [1, 2, 3], [{"op": "add", "path": "/items/-", "value": 3}]),
        (
            [1, 2, 3, 4],
            [1, 2],
            [{"op": "remove", "path": "/items/3"}, {"op": "remove", "path": "/items/2"}],
        ),
    ],
)
def test_append_and_tail_remove_records_are_byte_identical(old, new, operations):
    before, target = freeze_json({"items": old}), freeze_json({"items": new})
    entry = {"agent_id": "op", "status": "completed", "response": "ok"}
    expected = {
        "version": 3,
        "generation": GENERATION,
        "seq": 1,
        "kind": "op",
        "entry": entry,
        "flow_context": {"patch": operations},
    }
    assert encode_record(GENERATION, 1, "op", freeze_json(entry), diff_context(before, target)) == (
        compact(expected) + b"\n"
    )


def test_middle_remove_still_rebuilds_array(monkeypatch):
    before = freeze_json({"items": [0, 1, 2, 3]})
    rebuild = codec._array_from_values
    calls = []

    def observe_rebuild(values):
        calls.append(list(values))
        return rebuild(values)

    monkeypatch.setattr(codec, "_array_from_values", observe_rebuild)
    replayed = apply_patch(before, (PatchOp("remove", "/items/1"),))
    assert calls == [[0, 2, 3]]
    assert thaw_json(replayed) == {"items": [0, 2, 3]}
    assert thaw_json(before) == {"items": [0, 1, 2, 3]}


def test_context_capture_is_frozen_and_aliases_update_both_paths():
    shared = ["old"]
    live = {"left": shared, "right": shared, "stable": {"k": "v"}}
    first, _ = capture_context(live)
    shared.append("new")
    second, patch = capture_context(live, first)
    shared.append("later")
    assert first["stable"] is second["stable"]
    assert thaw_json(first) == {"left": ["old"], "right": ["old"], "stable": {"k": "v"}}
    assert thaw_json(second) == {
        "left": ["old", "new"],
        "right": ["old", "new"],
        "stable": {"k": "v"},
    }
    assert thaw_json(apply_patch(first, patch)) == thaw_json(second)
    assert {op.path for op in patch} == {"/left/-", "/right/-"}


def test_scalar_types_and_negative_zero_are_not_coalesced():
    old, _ = capture_context({"i": 1, "b": True, "f": 0.0})
    new, patch = capture_context({"i": 1.0, "b": 1, "f": -0.0}, old)
    assert {op.path for op in patch} == {"/i", "/b", "/f"}
    recovered = thaw_json(apply_patch(old, patch))
    assert type(recovered["i"]) is float
    assert type(recovered["b"]) is int
    assert json.dumps(recovered["f"]) == "-0.0"
    assert thaw_json(new) == recovered


def test_none_empty_and_null_are_distinct():
    old, _ = capture_context({"x": None})
    unchanged, empty_patch = capture_context({"x": None}, old)
    cleared, patch = capture_context({}, old)
    assert unchanged is old and empty_patch == ()
    assert patch == (PatchOp("remove", "/x"),)
    assert thaw_json(apply_patch(old, patch)) == thaw_json(cleared) == {}
    entry = freeze_json({"agent_id": "a", "status": "completed", "response": None})
    assert decode_record(encode_record(GENERATION, 1, "op", entry, None)).patch is None
    assert decode_record(encode_record(GENERATION, 1, "op", entry, ())).patch == ()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_and_keys_are_refused(bad):
    with pytest.raises(CodecError):
        freeze_json({"nested": [bad]})
    with pytest.raises(CodecError):
        freeze_json({bad: "bad"})


def test_key_collision_cycle_and_repeated_noncyclic_alias():
    with pytest.raises(CodecError):
        freeze_json({1: "a", "1": "b"})
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(CodecError):
        freeze_json({"a": cyclic})
    indirect = {}
    indirect["nested"] = [indirect]
    with pytest.raises(CodecError):
        freeze_json(indirect)
    shared = [1]
    assert thaw_json(freeze_json({"a": shared, "b": shared})) == {"a": [1], "b": [1]}


def test_fallback_is_frozen_once_and_failures_are_sanitized():
    class Item:
        def __init__(self):
            self.calls = 0

        def __str__(self):
            self.calls += 1
            return "captured"

    item = Item()
    result = freeze_json({"item": item})
    assert item.calls == 1 and thaw_json(result) == {"item": "captured"}

    class Broken:
        def __str__(self):
            raise ValueError("sensitive")

    with pytest.raises(CodecError) as caught:
        freeze_json({"x": Broken()})
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("token", ["-", "00", "01", "+1", "-1", " 1", "1 "])
def test_invalid_array_indices_are_refused(token):
    before = freeze_json({"x": [1, 2]})
    with pytest.raises(CodecError):
        apply_patch(before, (PatchOp("remove", f"/x/{token}"),))
    assert thaw_json(before) == {"x": [1, 2]}


def test_pointer_escape_and_array_insertions_are_sequential():
    before = freeze_json({"~/": {"~1": ["a", "c"], "-": "old"}})
    patch = (
        PatchOp("add", "/~0~1/~01/1", "b"),
        PatchOp("replace", "/~0~1/-", "new"),
        PatchOp("remove", "/~0~1/~01/0"),
    )
    after = apply_patch(before, patch)
    assert thaw_json(after) == {"~/": {"~1": ["b", "c"], "-": "new"}}
    assert thaw_json(before) == {"~/": {"~1": ["a", "c"], "-": "old"}}


@pytest.mark.parametrize("path", ["x", "/~", "/~2", "/foo/~3"])
def test_invalid_pointer_syntax_is_refused(path):
    with pytest.raises(CodecError):
        apply_patch(freeze_json({}), (PatchOp("add", path, "x"),))


def test_failed_late_patch_does_not_mutate_earlier_context():
    before = freeze_json({"arr": [1], "entry": "present"})
    patch = (PatchOp("add", "/arr/-", 2), PatchOp("remove", "/missing"))
    with pytest.raises(CodecError):
        apply_patch(before, patch)
    assert thaw_json(before) == {"arr": [1], "entry": "present"}


def test_root_replace_requires_an_object():
    before = freeze_json({"a": 1})
    assert thaw_json(apply_patch(before, (PatchOp("replace", "", freeze_json({})),))) == {}
    for op in (
        PatchOp("replace", "", None),
        PatchOp("add", "", freeze_json({})),
        PatchOp("remove", ""),
    ):
        with pytest.raises(CodecError):
            apply_patch(before, (op,))


@pytest.mark.parametrize(
    "text",
    [
        '{"a":1,"a":2}',
        '{"a":{"x":1,"x":2}}',
        '{"version":3,"vers\\u0069on":3}',
        '{"x":[{"z":1,"z":2}]}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e9999}',
    ],
)
def test_strict_json_refuses_duplicates_and_nonfinite_at_all_depths(text):
    with pytest.raises(CodecError):
        decode_json(text)


def test_decoder_requires_utf8_and_sanitizes_errors():
    with pytest.raises(CodecError):
        decode_json('{"k":"v"}'.encode("utf-16"))
    with pytest.raises(CodecError) as caught:
        decode_json('{"secret":"test-secret",')
    assert "test-secret" not in str(caught.value)


def test_object_member_add_replace_remove_and_empty_key():
    before = freeze_json({"": {"a": 1}})
    patch = (PatchOp("add", "//b", 2), PatchOp("replace", "//a", None), PatchOp("remove", "//b"))
    assert thaw_json(apply_patch(before, patch)) == {"": {"a": None}}
    with pytest.raises(CodecError):
        apply_patch(before, (PatchOp("add", "//a", 2),))


def test_spawned_entry_extensions_survive_encode_decode():
    entry = {
        "node_id": "node",
        "status": "failed",
        "response": None,
        "operation": "chat",
        "assignee": "worker",
        "instruction": "go",
        "parent_id": "parent",
        "spawn_id": "spawn",
        "context": {"private": ["x"]},
        "emitter_workload": "standard",
        "emitter_contains_user_settings": True,
        "extension": {"other": 2},
    }
    captured = freeze_json(entry)
    validate_entry("spawned", captured)
    record = decode_record(encode_record(GENERATION, 4, "spawned", captured))
    assert thaw_json(record.entry) == entry


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("version", True, "unsupported_version"),
        ("version", 2, "unsupported_version"),
        ("seq", True, "invalid_sequence"),
        ("seq", 0, "invalid_sequence"),
        ("seq", 2**63, "invalid_sequence"),
        ("kind", "unknown", "unsupported_kind"),
        ("generation", "bad", "invalid_record"),
    ],
)
def test_record_fields_are_validated_with_stable_reasons(field, value, reason):
    obj = {
        "version": 3,
        "generation": GENERATION,
        "seq": 1,
        "kind": "op",
        "entry": {"agent_id": "a", "status": "completed", "response": None},
    }
    obj[field] = value
    with pytest.raises(CodecError) as caught:
        decode_record(compact(obj) + b"\n")
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"agent_id": "", "status": "completed", "response": "x"},
        {"agent_id": "a", "status": "pending", "response": "x"},
        {"agent_id": "a", "status": "completed"},
    ],
)
def test_invalid_outcomes_are_rejected(entry):
    with pytest.raises(CodecError):
        encode_record(GENERATION, 1, "op", freeze_json(entry))


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "add", "path": "/a"}],
        [{"op": "remove", "path": "/a", "value": None}],
        [{"op": "test", "path": "/a", "value": 1}],
        [{"op": "add", "path": "/a", "value": 1, "extra": 2}],
    ],
)
def test_unknown_patch_operations_and_fields_are_rejected(patch):
    obj = {
        "version": 3,
        "generation": GENERATION,
        "seq": 1,
        "kind": "op",
        "entry": {"agent_id": "a", "status": "completed", "response": None},
        "flow_context": {"patch": patch},
    }
    with pytest.raises(CodecError):
        decode_record(compact(obj) + b"\n")


def test_invalid_or_missing_lf_cannot_be_confused_with_a_record():
    entry = freeze_json({"agent_id": "a", "status": "failed", "response": "x"})
    line = encode_record(GENERATION, 1, "op", entry)
    with pytest.raises(CodecError):
        decode_record(line[:-1])
    with pytest.raises(CodecError):
        decode_record(b"{invalid}\n")


def test_large_array_grows_by_a_right_spine_without_copying_prefix_chunks():
    before, _ = capture_context({"items": list(range(1024))})
    after, patch = capture_context({"items": [*range(1024), 1024]}, before)
    assert isinstance(before["items"], FrozenArray)
    assert isinstance(before, FrozenObject)
    assert before["items"]._root is after["items"]._root.children[0]
    assert len(patch) == 1
    assert thaw_json(apply_patch(before, patch)) == thaw_json(after)


def test_primitive_subclasses_keep_underlying_json_values_without_calling_overrides():
    class CustomInt(int):
        def __int__(self):
            raise ValueError("int conversion secret")

        def __repr__(self):
            raise ValueError("int repr secret")

    class CustomFloat(float):
        def __float__(self):
            raise ValueError("float conversion secret")

        def __repr__(self):
            raise ValueError("float repr secret")

    class CustomString(str):
        def __str__(self):
            raise ValueError("string conversion secret")

    live = {
        CustomString("name"): CustomString("original"),
        CustomInt(2): CustomInt(7),
        CustomFloat(3.5): CustomFloat(4.5),
    }
    captured = freeze_json(live)
    assert thaw_json(captured) == {"name": "original", "2": 7, "3.5": 4.5}


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_float_subclass_cannot_hide_a_nonfinite_value_or_key(number):
    class HidingFloat(float):
        def __float__(self):
            return 1.0

        def __str__(self):
            return "1.0"

    bad = HidingFloat(number)
    for value in ({"nested": bad}, {bad: "key"}):
        with pytest.raises(CodecError):
            freeze_json(value)


def test_capture_redacts_errors_raised_by_container_subclasses():
    class BrokenDict(dict):
        def items(self):
            raise RuntimeError("sensitive payload")

    with pytest.raises(CodecError) as caught:
        freeze_json(BrokenDict({"ok": 1}))
    assert "sensitive payload" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_frozen_equality_uses_normalized_json_types_and_not_trie_shape():
    assert freeze_json({"x": True}) != freeze_json({"x": 1})
    assert freeze_json({"x": 1}) != freeze_json({"x": 1.0})
    assert freeze_json({"x": 0.0}) != freeze_json({"x": -0.0})
    assert freeze_json({"a": 1, "b": 2}) == freeze_json({"b": 2, "a": 1})
    large, _ = capture_context({"x": list(range(1025))})
    shrunk, _ = capture_context({"x": [0]}, large)
    fresh, _ = capture_context({"x": [0]})
    assert shrunk["x"]._depth != fresh["x"]._depth
    assert shrunk == fresh
    assert PatchOp("add", "/x", True) != PatchOp("add", "/x", 1)
    first = JournalRecord(
        GENERATION, 1, "op", freeze_json({"agent_id": "a", "status": "completed", "response": True})
    )
    second = JournalRecord(
        GENERATION, 1, "op", freeze_json({"agent_id": "a", "status": "completed", "response": 1})
    )
    assert first != second


def test_direct_frozen_constructors_reject_mutable_or_invalid_payloads():
    with pytest.raises(CodecError):
        FrozenObject((("x", []),))
    with pytest.raises(CodecError):
        FrozenObject((("x", 1), ("x", 2)))
    with pytest.raises(CodecError):
        FrozenObject((("x", float("nan")),))
    with pytest.raises(CodecError):
        PatchOp("add", "/x", {"mutable": True})
    entry = freeze_json({"agent_id": "a", "status": "completed", "response": None})
    with pytest.raises(CodecError):
        JournalRecord(GENERATION, 1, "op", entry, [])


def test_record_decoder_rejects_internal_or_extra_lf():
    entry = freeze_json({"agent_id": "a", "status": "completed", "response": None})
    line = encode_record(GENERATION, 1, "op", entry)
    with pytest.raises(CodecError):
        decode_record(line.replace(b'{"version"', b'{\n"version"', 1))
    with pytest.raises(CodecError):
        decode_record(line + b"\n")


def test_capture_redacts_codec_errors_raised_by_user_containers():
    secret = "private-user-payload"

    class HostileDict(dict):
        def items(self):
            raise CodecError(secret)

    class HostileList(list):
        def __iter__(self):
            raise CodecError(secret)

    for value in (HostileDict(x=1), HostileList([1])):
        with pytest.raises(CodecError) as caught:
            freeze_json(value)
        assert caught.value.reason == "invalid_record"
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


def test_future_record_version_is_classified_before_v3_member_schema():
    obj = {
        "version": 4,
        "generation": GENERATION,
        "seq": 1,
        "kind": "op",
        "entry": {"agent_id": "a", "status": "completed", "response": "ok"},
        "future_field": {"option": True},
    }
    with pytest.raises(CodecError) as caught:
        decode_record(compact(obj) + b"\n")
    assert caught.value.reason == "unsupported_version"
    obj["version"] = 3
    with pytest.raises(CodecError) as caught:
        decode_record(compact(obj) + b"\n")
    assert caught.value.reason == "invalid_record"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":4,"version":4,"future_field":true}\n',
        b'{"version":4,"future_field":{"number":NaN}}\n',
    ],
)
def test_future_version_does_not_bypass_strict_json_validation(payload):
    with pytest.raises(CodecError) as caught:
        decode_record(payload)
    assert caught.value.reason == "invalid_record"


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (PatchOp("replace", "/a", 3), [("a", 3), ("b", 2)]),
        (PatchOp("add", "/c", 3), [("a", 1), ("b", 2), ("c", 3)]),
        (PatchOp("remove", "/a"), [("b", 2)]),
    ],
    ids=["replace-preserves-position", "add-appends", "remove-deletes"],
)
def test_patch_preserves_object_member_order(operation, expected):
    before = freeze_json({"a": 1, "b": 2})
    after = apply_patch(before, (operation,))
    assert list(thaw_json(after).items()) == expected
    assert list(thaw_json(before).items()) == [("a", 1), ("b", 2)]


def test_pure_reorder_is_not_replayed():
    base = freeze_json({"a": 1, "b": 2})
    target = freeze_json({"b": 2, "a": 1})
    ops = diff_context(base, target)
    replayed = apply_patch(base, ops)
    assert base == target
    assert ops == ()
    assert list(thaw_json(replayed)) == ["a", "b"]
    assert list(thaw_json(target)) == ["b", "a"]
