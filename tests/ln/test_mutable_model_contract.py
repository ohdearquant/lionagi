# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mutable model admission, ordered deduplication, and compatibility contracts."""

from __future__ import annotations

import json
import operator
import sys
from array import array
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, field_serializer

from lionagi.libs.schema.function_to_schema import FunctionSchema
from lionagi.ln import to_list
from lionagi.ln.types import DataClass, Meta, Params, Spec
from lionagi.models import HashableModel, OperableModel, SchemaModel
from lionagi.operations import fields
from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis
from lionagi.protocols.messages.action_request import ActionRequestContent
from lionagi.protocols.messages.action_response import ActionResponseContent
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.protocols.messages.instruction import InstructionContent
from lionagi.protocols.messages.message import MessageContent
from lionagi.protocols.messages.system import SystemContent


@dataclass(slots=True, eq=False)
class MutableContext(DataClass):
    values: list[int] = field(default_factory=list)


# The fieldless DataClass base is checked directly for container admission. Its
# supported mutation contract is exercised through a declared-field subclass.
# Fieldless Pydantic bases permit private instance state without extra wire fields.
MUTATION_CASES = [
    (DataClass, DataClass, None, None, None),
    (MessageContent, MessageContent, "_revision", 0, 1),
    (AssistantResponseContent, AssistantResponseContent, "assistant_response", "", "changed"),
    (ActionRequestContent, ActionRequestContent, "function", "", "changed"),
    (ActionResponseContent, ActionResponseContent, "output", None, "changed"),
    (InstructionContent, InstructionContent, "instruction", None, "changed"),
    (SystemContent, SystemContent, "system_message", "before", "after"),
    (HashableModel, HashableModel, "_mutable_state", [1], [2]),
    (OperableModel, OperableModel, "extra_fields", {}, {}),
    (SchemaModel, SchemaModel, "_mutable_state", [1], [2]),
    (FunctionSchema, lambda: FunctionSchema(name="before"), "name", "before", "after"),
    (fields.Instruct, fields.Instruct, "instruction", None, "changed"),
    (fields.Reason, fields.Reason, "title", None, "changed"),
    (fields.ActionRequestModel, fields.ActionRequestModel, "function", None, "changed"),
    (fields.ActionResponseModel, fields.ActionResponseModel, "output", None, "changed"),
    (ReActAnalysis, ReActAnalysis, "analysis", "", "changed"),
    (Analysis, Analysis, "answer", None, "changed"),
]


def test_runtime_mutation_cases_cover_the_closed_inventory():
    inventory = json.loads(
        (Path(__file__).parent / "data/mutable_model_inventory.json").read_text()
    )
    fixtures = {f"{case[0].__module__}.{case[0].__qualname__}" for case in MUTATION_CASES}
    assert len(fixtures) == len(MUTATION_CASES)
    assert fixtures == {entry["name"] for entry in inventory["types"]}
    assert fixtures == {entry["runtime_fixture"] for entry in inventory["types"]}


def _reject_container_admission(value):
    for admit in (hash, lambda item: {item}, lambda item: {item: "value"}):
        with pytest.raises(TypeError, match="unhashable type"):
            admit(value)


@pytest.mark.parametrize(
    ("model_type", "factory", "attribute", "before", "after"),
    MUTATION_CASES,
    ids=[f"{entry[0].__module__}.{entry[0].__qualname__}" for entry in MUTATION_CASES],
)
def test_mutable_family_refuses_container_admission_and_remains_mutable(
    model_type, factory, attribute, before, after
):
    instance = factory()
    assert model_type.__hash__ is None
    _reject_container_admission(instance)

    if model_type is DataClass:
        instance = MutableContext([1])
        instance.values.append(2)
        assert instance.values == [1, 2]
    elif model_type is OperableModel:
        from lionagi.models import FieldModel

        instance.add_field("counter", field_model=FieldModel(int, default=1))
        assert instance.counter == 1
        instance.counter = 2
        assert instance.counter == 2
    else:
        setattr(instance, attribute, before)
        assert getattr(instance, attribute) == before
        setattr(instance, attribute, after)
        assert getattr(instance, attribute) == after
    _reject_container_admission(instance)


@pytest.mark.parametrize(
    ("model_type", "factory"),
    [(entry[0], entry[1]) for entry in MUTATION_CASES],
    ids=[f"{entry[0].__module__}.{entry[0].__qualname__}" for entry in MUTATION_CASES],
)
def test_mutable_family_serialization_roundtrips(model_type, factory):
    instance = factory()
    if isinstance(instance, DataClass):
        payload = instance.to_dict(mode="json")
        restored = model_type(**payload)
    else:
        payload = instance.to_dict()
        restored = model_type.from_json(instance.to_json())
    assert restored is not instance
    assert restored == instance
    assert restored.to_dict() == payload


def test_mutable_dataclass_equality_and_updates_preserve_exact_type():
    @dataclass(slots=True, eq=False)
    class OtherContext(DataClass):
        values: list[int]

    first = MutableContext([1])
    equal = MutableContext([1])
    assert first == equal
    assert first != OtherContext([1])
    equal.values.append(2)
    assert first != equal
    updated = first.with_updates(values=[3])
    assert updated.to_dict() == {"values": [3]}
    assert first.to_dict() == {"values": [1]}


def test_message_generated_equality_remains_in_force():
    first = ActionResponseContent(output=[1])
    equal = ActionResponseContent(output=[1])
    equal._revision = first._revision + 1
    assert first == equal  # The revision field declares compare=False.
    assert ActionResponseContent.__eq__ is not DataClass.__eq__
    equal.output.append(2)
    assert first != equal


def _unexpected_hash(_value):
    raise AssertionError("Mutable values must not be content-hashed here")


def test_generic_dataclass_dedup_uses_values_without_hashing(monkeypatch):
    monkeypatch.setattr(DataClass, "__hash__", _unexpected_hash)
    monkeypatch.setattr(import_module("lionagi.ln._to_list"), "hash_dict", _unexpected_hash)
    first, duplicate, distinct = MutableContext([1]), MutableContext([1]), MutableContext([2])
    result = to_list([first, [duplicate, distinct], first], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is distinct


def test_generic_dataclass_dedup_accepts_generated_equality_and_cycles():
    @dataclass(slots=True)
    class GeneratedContext(DataClass):
        value: str

    first = GeneratedContext("same")
    other = GeneratedContext("different")
    cyclic = MutableContext()
    cyclic.values.append(cyclic.values)
    result = to_list(
        [first, GeneratedContext("same"), other, cyclic, cyclic], flatten=True, unique=True
    )
    assert len(result) == 3
    assert result[0] is first
    assert result[1] is other
    assert result[2] is cyclic


@dataclass(slots=True, eq=False)
class Point(DataClass):
    x: int = 0


@dataclass(slots=True, eq=False)
class LabelledPoint(Point):
    """Equal to any point at the same position, whatever its label."""

    label: str = ""

    def __eq__(self, other):
        return isinstance(other, Point) and other.x == self.x

    __hash__ = None


@pytest.mark.parametrize("labelled_first", [False, True], ids=["base-first", "labelled-first"])
def test_dataclass_dedup_finds_a_pair_its_own_equality_calls_equal_in_either_order(
    labelled_first,
):
    point, labelled, other = Point(1), LabelledPoint(1, "a"), Point(2)
    first, second = (labelled, point) if labelled_first else (point, labelled)
    result = to_list([first, second, LabelledPoint(1, "b"), other], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is other


@dataclass(slots=True, eq=False)
class HashableLabelledPoint(Point):
    """Equal to any point at the same position, and hashable by its position alone."""

    label: str = ""

    def __eq__(self, other):
        return isinstance(other, Point) and other.x == self.x

    def __hash__(self):
        return hash(self.x)


@pytest.mark.parametrize("labelled_first", [False, True], ids=["base-first", "labelled-first"])
def test_dataclass_dedup_keeps_equal_values_whose_hashes_differ(labelled_first):
    """Deduplication keeps Python's set semantics for values that carry a hash: two
    values whose hashes differ land in different buckets and are never compared, so a
    type whose ``__eq__`` crosses kinds without a matching ``__hash__`` keeps both of
    its equal values, in either order, exactly as adding both to a ``set`` would.
    """
    exposed = import_module("lionagi.ln._to_list")
    placed = Point(1)
    labelled = HashableLabelledPoint(1, "a")
    assert placed == labelled
    assert labelled == placed
    # The base-equality value is grouped by its field state, the other by its own
    # hash; the two keys differ, so the pair is never compared.
    assert exposed._dataclass_content_key(placed) != exposed._own_content_key(labelled)
    first, second = (labelled, placed) if labelled_first else (placed, labelled)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is second


@dataclass(slots=True, eq=False)
class Loose(DataClass):
    """Equal to any plain value, whatever its own field holds."""

    tag: int = 0

    def __eq__(self, other):
        return isinstance(other, Point)

    __hash__ = None


@pytest.mark.parametrize("loose_first", [False, True], ids=["plain-first", "loose-first"])
def test_dataclass_dedup_finds_a_sibling_pair_its_own_equality_calls_equal_in_either_order(
    loose_first,
):
    plain, loose = Point(1), Loose(0)
    first, second = (loose, plain) if loose_first else (plain, loose)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is first


@dataclass(slots=True, eq=False)
class LooseHolder(DataClass):
    """Equal to any holder, whatever its own field holds."""

    tag: int = 0

    def __eq__(self, other):
        return isinstance(other, Holder)

    __hash__ = None


@pytest.mark.parametrize("loose_first", [False, True], ids=["holder-first", "loose-first"])
def test_dataclass_dedup_finds_an_unkeyable_pair_its_own_equality_calls_equal_in_either_order(
    loose_first,
):
    holder, loose = Holder(memoryview(bytearray(b"ab"))), LooseHolder(0)
    first, second = (loose, holder) if loose_first else (holder, loose)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is first


@dataclass(slots=True, eq=False)
class Keyed(DataClass):
    """Its own equality, and its own hash, read a field the other value may not carry."""

    k: int = 0

    def __eq__(self, other):
        return self.k == other.k

    def __hash__(self):
        return hash(self.k)


@dataclass(slots=True, eq=False)
class SelfHashedChild(DataClass):
    """Its own equality is identity, and its own hash reads its own child."""

    child: object = None

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return hash(self.child)


def test_dataclass_dedup_does_not_hide_a_hash_that_reads_absent_state():
    """A value whose own ``__hash__`` reads state it no longer carries is a bug in that
    ``__hash__``: only ``TypeError`` means "unhashable", so this one propagates rather
    than being swallowed as a keyless value."""

    deleted = Keyed(1)
    del deleted.k
    with pytest.raises(AttributeError):
        to_list([deleted], flatten=True, unique=True)
    with pytest.raises(AttributeError):
        fields._validate_listable_field(None, [deleted])


def test_dataclass_dedup_does_not_hide_a_hash_that_recurses_without_end():
    """A value whose own ``__hash__`` descends into a cycle without end raises
    ``RecursionError``; that is a bug in the ``__hash__`` and it propagates."""

    cyclic = SelfHashedChild()
    cyclic.child = cyclic
    with pytest.raises(RecursionError):
        to_list([cyclic], flatten=True, unique=True)
    with pytest.raises(RecursionError):
        fields._validate_listable_field(None, [cyclic])


def _cyclic_output():
    cyclic: list = []
    cyclic.append(cyclic)
    return fields.ActionResponseModel(output=cyclic)


class _UnserializableMapping(dict):
    """A mapping whose contents cannot be read for a structural key."""

    def items(self):
        raise TypeError("contents cannot be read")


class _ValueErrorMapping(dict):
    """A mapping whose contents report a defect with ``ValueError``."""

    def items(self):
        raise ValueError("contents cannot be read")


class TaggedEqual(HashableModel):
    """Equal to any tagged value at the same ``x``, whatever else it holds."""

    x: int = 1
    extra: object = None

    def __eq__(self, other):
        return isinstance(other, TaggedEqual) and self.x == other.x


@pytest.mark.parametrize("keyed_first", [False, True], ids=["keyed-first", "keyless-first"])
def test_mapping_dedup_compares_a_keyless_mapping_with_a_keyable_equal_one(keyed_first):
    """A mapping whose content key cannot be taken is compared against every kept value,
    keyed mappings included, so equal contents are one value in either order. A keyed
    mapping is compared against the keyless values as well, so neither order keeps it
    twice."""

    keyable, keyless = {"a": 1}, _UnserializableMapping(a=1)
    assert keyable == keyless
    first, second = (keyable, keyless) if keyed_first else (keyless, keyable)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is first


def test_a_mapping_whose_contents_raise_valueerror_propagates():
    """A mapping's contents raising ``ValueError`` is a defect in that mapping, not an
    unkeyable value: the error propagates from deduplication instead of the mapping
    being compared only by equality."""
    broken = _ValueErrorMapping(a=1)
    with pytest.raises(ValueError, match="contents cannot be read"):
        to_list([broken, dict(broken)], flatten=True, unique=True)


@pytest.mark.parametrize("keyed_first", [False, True], ids=["keyed-first", "keyless-first"])
def test_mapping_dedup_compares_a_keyless_model_with_a_keyable_equal_one(keyed_first):
    """A Pydantic model is a mapping-like value whose content key follows its own
    equality, so the same rule reads a keyable and a keyless instance as one value."""

    cyclic: list = []
    cyclic.append(cyclic)
    keyable, keyless = TaggedEqual(x=1), TaggedEqual(x=1, extra=cyclic)
    assert keyable == keyless
    first, second = (keyable, keyless) if keyed_first else (keyless, keyable)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is first


def _cyclic_mapping():
    mapping: dict = {}
    mapping["self"] = mapping
    return mapping


def _distinct_cyclic_outputs():
    first: list = []
    first.append(first)
    second: list = []
    second.append(second)
    return fields.ActionResponseModel(output=first), fields.ActionResponseModel(output=second)


def test_dedup_keeps_two_distinct_cyclic_mappings_instead_of_raising():
    """Two distinct cyclic mappings raise ``RecursionError`` from comparing them; that
    comparison counts as unequal, so both are kept rather than the error escaping."""

    first, second = _cyclic_mapping(), _cyclic_mapping()
    assert to_list([first, second], flatten=True, unique=True) == [first, second]
    assert fields._validate_listable_field(None, [first, second]) == [first, second]


def test_dedup_keeps_two_distinct_cyclic_models_instead_of_raising():
    """Two distinct models whose output recurses compare with an unbounded descent; the
    comparison counts as unequal and both are kept."""

    first, second = _distinct_cyclic_outputs()
    assert to_list([first, second], flatten=True, unique=True) == [first, second]
    assert fields._validate_listable_field(None, [first, second]) == [first, second]


class _CyclicStruct(msgspec.Struct):
    """An msgspec struct whose field can hold the struct itself."""

    child: object = None


class _CyclicModel(BaseModel):
    """A Pydantic model whose field can hold the model itself."""

    child: _CyclicModel | None = None


_CyclicModel.model_rebuild()


def test_a_cyclic_struct_reports_unhashable_from_its_content_key():
    """``msgspec.to_builtins`` descends into a struct that holds itself and reaches the
    recursion limit before the content hash can walk it, so the content key reports the
    same unhashable ``TypeError`` a cyclic container reports and the equality path
    takes it."""
    from lionagi.ln import hash_dict

    cyclic = _CyclicStruct()
    cyclic.child = cyclic
    with pytest.raises(TypeError, match="cannot key a cyclic or too deeply nested"):
        hash_dict(cyclic)
    assert to_list([cyclic, cyclic], flatten=True, unique=True) == [cyclic]


def test_a_cyclic_model_reports_unhashable_from_its_content_key():
    """Pydantic's ``model_dump`` refuses a model that holds itself with ``ValueError``;
    that is read as the same unhashable state and the equality path takes it."""
    from lionagi.ln import _hash

    cyclic = _CyclicModel()
    cyclic.child = cyclic
    with pytest.raises(TypeError, match="cannot key a cyclic or too deeply nested"):
        _hash._generate_hashable_representation(cyclic)


@pytest.mark.parametrize("kind", ["pydantic", "msgspec"])
def test_a_cyclic_model_field_compares_unequal_under_the_base_equality(kind):
    """Pydantic's ``model_dump`` refuses a model that holds itself with ``ValueError``
    and ``msgspec.to_builtins`` with ``RecursionError``, before the base equality can
    walk it; either is read as the cycle the projection reports on its own path, so the
    values compare unequal, as every other cyclic value does, and no stable cache key
    is kept."""
    from lionagi.ln import _structural

    def cyclic():
        value = _CyclicModel() if kind == "pydantic" else _CyclicStruct()
        value.child = value
        return value

    first, second = cyclic(), cyclic()
    assert (MutableContext([first]) == MutableContext([second])) is False
    assert _structural._try_stable_cache_key(first) is None
    with pytest.raises(_structural.UnhashableStructuralValueError):
        _structural._dataclass_structural_key(first)


def test_a_value_nested_past_the_recursion_limit_falls_back_to_equality():
    """A value that nests past the recursion limit raises ``RecursionError`` while its
    content key is built; the keying falls back to the equality path, so two equal
    values are one instead of the error escaping ``to_list``.

    The two values share one nested list, so their comparison takes the identity
    shortcut and never recurses. Two separate nested lists of this depth cannot be
    compared on Python 3.10 and 3.11, where that comparison raises ``RecursionError``
    and reads as unequal."""
    inner = _nested(sys.getrecursionlimit())
    deep = {"x": inner}
    assert len(to_list([deep, dict(deep)], flatten=True, unique=True)) == 1
    first = fields.ActionResponseModel(output=inner)
    second = fields.ActionResponseModel(output=inner)
    assert first is not second
    assert len(to_list([first, second], flatten=True, unique=True)) == 1


def test_structural_hash_of_a_cyclic_value_reports_unhashable():
    """A cyclic container reports ``TypeError`` from the content key instead of
    descending to the recursion limit, the same failure a value reports when it cannot
    be keyed."""

    from lionagi.ln import hash_dict

    for cyclic in (_cyclic_mapping(),):
        with pytest.raises(TypeError, match="cyclic"):
            hash_dict(cyclic)
    self_referencing: list = []
    self_referencing.append(self_referencing)
    with pytest.raises(TypeError, match="cyclic"):
        hash_dict(self_referencing)


def test_structural_hash_of_a_value_that_repeats_a_shared_object_is_unchanged():
    """A value that holds the same object twice without holding itself is not cyclic:
    its representation is built, and it equals the representation of a copy."""

    from lionagi.ln import hash_dict

    shared = [1, 2]
    assert hash_dict({"a": shared, "b": shared}) == hash_dict({"a": [1, 2], "b": [1, 2]})


def test_dedup_falls_back_to_equality_when_a_model_cannot_be_keyed():
    """A model whose output recurses cannot be hashed at all, in the plain unique
    branch or through the operation validator, and is deduplicated by identity."""

    cyclic = _cyclic_output()
    assert to_list([cyclic, cyclic], flatten=True, unique=True) == [cyclic]
    assert fields._validate_listable_field(None, [cyclic, cyclic]) == [cyclic]


@dataclass(slots=True, eq=False)
class Strict(DataClass):
    """Its own equality, without a hash, reads a field the other value may not carry."""

    k: int = 0

    def __eq__(self, other):
        return self.k == other.k

    __hash__ = None


@pytest.mark.parametrize("keyed_first", [False, True], ids=["plain-first", "keyed-first"])
def test_dataclass_dedup_reads_an_unreadable_hashable_equality_as_unequal_in_either_order(
    keyed_first,
):
    """An ``AttributeError`` from a value's own ``__eq__`` reads as unequal, as the
    base equality reads its own uncomparable state, so both values are kept."""

    keyed, plain = Keyed(1), Point(1)
    first, second = (keyed, plain) if keyed_first else (plain, keyed)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is second


@pytest.mark.parametrize("strict_first", [False, True], ids=["plain-first", "strict-first"])
def test_dataclass_dedup_reads_an_unreadable_unhashable_equality_as_unequal_in_either_order(
    strict_first,
):
    strict, plain = Strict(1), Point(1)
    first, second = (strict, plain) if strict_first else (plain, strict)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is second


@dataclass(slots=True, eq=False)
class Carries(DataClass):
    """Its own equality reads a field only some values carry, through a name."""

    k: int = 0

    def __eq__(self, other):
        return self.k == other.k

    def __hash__(self):
        return hash(self.k)


@dataclass(slots=True, eq=False)
class KeyedPoint(Point):
    """Equal to a ``Point`` at the same position, and it reads ``other.k``."""

    k: int = 0

    def __eq__(self, other):
        return isinstance(other, KeyedPoint) and other.k == self.k

    def __hash__(self):
        return hash(self.k)


@pytest.mark.parametrize("keyed_first", [False, True], ids=["point-first", "keyed-first"])
def test_dataclass_dedup_keeps_a_cross_class_pair_whose_equality_reads_back(keyed_first):
    """A value whose own equality reads a field the other value lacks must not make the
    comparison raise: it reads as unequal, so both values are kept in either order."""

    plain, keyed = Point(1), KeyedPoint(1, 7)
    first, second = (keyed, plain) if keyed_first else (plain, keyed)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is second


@pytest.mark.parametrize("keyed_first", [False, True], ids=["point-first", "keyed-first"])
def test_operation_dedup_keeps_a_pair_whose_output_reads_back(keyed_first):
    plain = fields.ActionResponseModel(function="f", output=Point(1))
    keyed = fields.ActionResponseModel(function="f", output=KeyedPoint(1, 7))
    ordered = (keyed, plain) if keyed_first else (plain, keyed)
    result = fields._validate_listable_field(None, list(ordered))
    assert len(result) == 2
    assert result[0] is ordered[0]
    assert result[1] is ordered[1]


@dataclass(slots=True, eq=False)
class Incomparable(DataClass):
    """Its own equality refuses a pair it cannot compare, with ``ValueError``."""

    n: int = 0

    def __eq__(self, other):
        raise ValueError("incomparable values")

    __hash__ = None


@pytest.mark.parametrize("refusing_first", [False, True], ids=["plain-first", "refusing-first"])
def test_dataclass_dedup_reads_a_refused_comparison_as_unequal_in_either_order(
    refusing_first,
):
    refusing, plain = Incomparable(1), Point(1)
    first, second = (refusing, plain) if refusing_first else (plain, refusing)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is second


@dataclass(slots=True, eq=False)
class ArrayHolder(DataClass):
    """Its own equality asks an array for one truth value, which raises."""

    value: object = None

    def __eq__(self, other):
        return isinstance(other, ArrayHolder) and bool(self.value == other.value)

    __hash__ = None


@pytest.mark.parametrize(
    "other_first", [False, True], ids=["first-value-first", "second-value-first"]
)
def test_dataclass_dedup_never_truth_tests_an_elementwise_comparison(other_first):
    """A comparison with no single truth value keeps the values apart, in either order."""

    numpy = pytest.importorskip("numpy")
    one, two = ArrayHolder(numpy.array([1, 2])), ArrayHolder(numpy.array([1, 2]))
    first, second = (two, one) if other_first else (one, two)
    result = to_list([first, second], flatten=True, unique=True)
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is second


_READS = {"count": 0}


class ReadsOnce(Point):
    """Its field state reads once and then fails, as a value being written would."""

    def _field_state(self):
        _READS["count"] += 1
        if _READS["count"] > 1:
            raise AttributeError("field state is gone on the second read")
        return DataClass._field_state(self)


def test_dataclass_dedup_keeps_one_value_when_a_repeat_cannot_be_keyed_twice():
    """Identity covers every kept value, so a repeat is dropped before its field state
    is read a second time."""

    _READS["count"] = 0
    value = ReadsOnce(1)
    result = to_list([value, value], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is value


class ReadsFourTimes(Point):
    """Its field state reads four times and then fails, as a value being written would.

    Four reads are exactly what the first copy costs for its key and the second for its
    key and the equality that drops it; a value recorded only when it is kept reads a
    fifth time for the repeated copy and fails.
    """

    def _field_state(self):
        _READS["count"] += 1
        if _READS["count"] > 4:
            raise AttributeError("field state is gone after its fourth read")
        return DataClass._field_state(self)


def test_dataclass_dedup_records_a_dropped_value_so_a_repeat_is_never_reread():
    """A value is recorded by identity whether it is kept or dropped, so a repeat of a
    value that equality already dropped is not keyed again. The first copy costs one read
    for its key, the second one more for its key and two for the equality that drops it,
    and the third is dropped by the recorded identity without a fifth read."""

    kept, dropped = ReadsFourTimes(1), ReadsFourTimes(1)
    assert kept == dropped
    _READS["count"] = 0  # Count only the reads the deduplication itself makes.
    result = to_list([kept, dropped, dropped], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is kept
    assert _READS["count"] == 4


class Pe:
    __hash__ = None

    def __eq__(self, other):
        return False


class Qe:
    __hash__ = None

    def __eq__(self, other):
        return isinstance(other, Pe)


@pytest.mark.parametrize("pe_first", [False, True], ids=["qe-first", "pe-first"])
def test_operation_dedup_keeps_one_value_for_one_sided_equality_in_either_order(pe_first):
    first = fields.ActionResponseModel(output=Pe())
    second = fields.ActionResponseModel(output=Qe())
    ordered = (first, second) if pe_first else (second, first)
    result = fields._validate_listable_field(None, list(ordered))
    assert len(result) == 1
    assert result[0] is ordered[0]


@dataclass(slots=True, unsafe_hash=True)
class FastKey(DataClass):
    """A hashable value whose generated equality follows its fields."""

    value: str = ""


def test_dataclass_dedup_keeps_hashable_values_in_input_order_quickly():
    """A few thousand hashable values deduplicate in well under a second."""

    import time

    values = [FastKey(str(n)) for n in range(6000)]
    doubled = values + values
    started = time.perf_counter()
    result = to_list([doubled], flatten=True, unique=True)
    elapsed = time.perf_counter() - started
    assert len(result) == len(values)
    assert all(kept is value for kept, value in zip(result, values, strict=True))
    assert elapsed < 2.0, f"deduplicating {len(doubled)} values took {elapsed:.2f}s"


@dataclass(slots=True, eq=False)
class Holder(DataClass):
    value: object = None


def _nested(depth):
    value = []
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    "make",
    [lambda: memoryview(bytearray(b"ab")), lambda: _nested(sys.getrecursionlimit())],
    ids=["writable-memoryview", "past-the-recursion-limit"],
)
def test_dataclass_dedup_keeps_a_value_whose_structure_has_no_content_key(make):
    """A writable memoryview's values can change, so it is keyed by identity as any
    mutable buffer is, and a value nested past the recursion limit cannot be keyed at
    all: either way a value is kept unless it is the same object as a kept value or a
    kept value's own equality calls it equal, and the values around it are deduplicated."""
    first, equal_content, plain = Holder(make()), Holder(make()), Holder("plain")
    result = to_list(
        [first, plain, equal_content, first, Holder("plain")], flatten=True, unique=True
    )
    assert len(result) == 3
    assert result[0] is first
    assert result[1] is plain
    assert result[2] is equal_content


class FrozenMemoryHolder(HashableModel, frozen=True):
    """A frozen model whose only field holds a writable memoryview."""

    value: object = None


@dataclass(slots=True, unsafe_hash=True)
class UnsafeHashHolder(DataClass):
    """Its own generated hash reaches a field that hashes with ``ValueError``."""

    value: object = None


class ValueErrorHash:
    """A value whose own ``__hash__`` reports a defect with ``ValueError``."""

    def __hash__(self):
        raise ValueError("a broken hash")


def _memoryviews():
    return memoryview(bytearray(b"ab")), memoryview(bytearray(b"ab"))


def test_a_writable_memoryview_element_is_flattened_not_hashed():
    """A writable memoryview is iterable, so a listable field flattens it to its bytes
    before any hash is taken; the equal bytes deduplicate to one copy."""
    first, second = _memoryviews()
    assert fields._validate_listable_field(None, [first, second]) == [97, 98]


def test_a_writable_memoryview_in_a_tuple_falls_back_to_equality():
    """A tuple holds its memoryview, so keying the tuple hashes the memoryview, which
    refuses with ``ValueError``; read as unhashable, the equal tuples deduplicate to
    one value instead of raising."""
    first, second = _memoryviews()
    result = fields._validate_listable_field(None, [(first,), (second,)])
    assert len(result) == 1
    assert result[0] == (first,)


def test_a_writable_memoryview_field_of_a_self_hashed_dataclass_falls_back_to_equality():
    """A ``DataClass`` whose own generated hash reaches a memoryview field refuses with
    ``ValueError``; read as unhashable, two equal values deduplicate to one."""
    first, second = _memoryviews()
    result = to_list([UnsafeHashHolder(first), UnsafeHashHolder(second)], flatten=True, unique=True)
    assert len(result) == 1
    assert result[0].value == first


def test_a_writable_memoryview_field_of_a_frozen_model_falls_back_to_equality():
    """A frozen model hashes its field values, so a memoryview field makes its hash
    refuse with ``ValueError``; the value takes the equality path, where its content
    key reads the memoryview by the bytes it holds, so two models holding distinct but
    equal memoryviews are one value, as the listable field reads them."""
    first, second = _memoryviews()
    holders = [FrozenMemoryHolder(value=first), FrozenMemoryHolder(value=second)]
    assert holders[0] == holders[1]
    result = to_list(holders, flatten=True, unique=True)
    assert len(result) == 1
    assert result[0] is holders[0]
    assert len(fields._validate_listable_field(None, holders)) == 1


def test_a_memoryview_is_keyed_by_the_bytes_it_holds():
    """A memoryview's string is its address, so keying by it would split views that
    compare equal; its content key is the one its bytes have, writable or read-only."""
    from lionagi.ln import hash_dict

    first, second = _memoryviews()
    assert hash_dict({"v": first}) == hash_dict({"v": second}) == hash_dict({"v": b"ab"})
    assert hash_dict({"v": memoryview(b"ab")}) == hash_dict({"v": b"ab"})
    assert hash_dict({"v": first}) != hash_dict({"v": memoryview(bytearray(b"ba"))})


def test_a_writable_memoryview_content_compares_by_identity():
    """A writable memoryview's values can change, so the base equality keys it by
    identity, as it keys a ``bytearray``: two ``DataClass`` values holding the same view
    are equal, and two holding distinct views are not, whatever the views hold."""
    first, second = _memoryviews()
    assert MutableContext([first]) == MutableContext([first])
    assert (MutableContext([first]) == MutableContext([second])) is False
    assert (MutableContext([second]) == MutableContext([first])) is False


def test_a_user_hash_that_raises_valueerror_still_propagates():
    """Only the writable-memoryview refusal is read as unhashable; a ``ValueError`` a
    value's own ``__hash__`` raises is a defect in that ``__hash__`` and propagates."""
    with pytest.raises(ValueError, match="a broken hash"):
        to_list([ValueErrorHash()], flatten=True, unique=True)
    with pytest.raises(ValueError, match="a broken hash"):
        fields._validate_listable_field(None, [ValueErrorHash()])


def test_a_user_hash_that_raises_valueerror_inside_a_field_propagates():
    """The base equality keys field contents with value equality, so a field value whose
    own ``__hash__`` raises ``ValueError`` reaches that hash; the error is a defect in
    that ``__hash__``, so it propagates from the equality and from deduplication rather
    than reading as an unhashable state."""
    with pytest.raises(ValueError, match="a broken hash"):
        _ = MutableContext([ValueErrorHash()]) == MutableContext([ValueErrorHash()])
    with pytest.raises(ValueError, match="a broken hash"):
        to_list(
            [MutableContext([ValueErrorHash()]), MutableContext([ValueErrorHash()])],
            flatten=True,
            unique=True,
        )


def _byte_views():
    """Two read-only views holding the values 97 and 98 in two formats, and one view
    holding other values. The two-byte view is built from its own values, so it holds
    them in either byte order."""
    return (
        memoryview(b"ab"),
        memoryview(array("H", [97, 98]).tobytes()).cast("H"),
        memoryview(b"ba"),
    )


def _released_view():
    view = memoryview(b"ab")
    view.release()
    return view


def test_a_fixed_memoryview_is_keyed_by_the_shape_and_values_it_holds():
    """A read-only view of ``bytes`` cannot change, so a frozen value holding one is
    hashable, and it equals one holding a view of the same shape and values in any
    format, as the views themselves do; other values or another shape are another key."""
    byte_view, short_view, other = _byte_views()
    assert byte_view == short_view
    first, second = Meta("k", byte_view), Meta("k", short_view)
    assert first == second
    assert hash(first) == hash(second)
    assert Meta("k", other) != first
    assert Meta("k", byte_view.cast("B", (1, 2))) != first
    assert len(to_list([first, second, Meta("k", other)], flatten=True, unique=True)) == 2


@pytest.mark.parametrize(
    "make",
    [
        lambda: memoryview(bytearray(b"ab")),
        lambda: memoryview(bytearray(b"ab")).toreadonly(),
        _released_view,
    ],
    ids=["writable", "read-only-over-a-mutable-buffer", "released"],
)
def test_a_memoryview_whose_values_can_change_makes_a_frozen_value_unhashable(make):
    """A writable view, a read-only view of a mutable buffer and a released view do not
    hold fixed values, so a frozen value holding one refuses its hash, as one holding a
    ``bytearray`` does, and still equals a value holding the same view."""
    view = make()
    holder = Meta("k", view)
    with pytest.raises(TypeError):
        hash(holder)
    assert holder == Meta("k", view)


def test_the_base_equality_reads_a_one_dimensional_byte_view_as_the_bytes_it_equals():
    """The base equality compares field values by value, and a one-dimensional view of
    byte values equals the bytes holding them, whatever its format; a view of another
    shape does not."""
    byte_view, short_view, _ = _byte_views()
    assert MutableContext([byte_view]) == MutableContext([b"ab"])
    assert MutableContext([short_view]) == MutableContext([b"ab"])
    assert MutableContext([byte_view.cast("B", (1, 2))]) != MutableContext([b"ab"])


@pytest.mark.parametrize(
    "make",
    [
        lambda: memoryview(bytearray(b"ab")),
        lambda: memoryview(array("H", [97, 98]).tobytes()).cast("H"),
        _released_view,
    ],
    ids=["writable", "non-byte-format", "released"],
)
def test_a_tuple_whose_memoryview_refuses_its_hash_falls_back_to_equality(make):
    """Hashing a tuple hashes its memoryview, which refuses with ``ValueError`` when the
    view is writable, holds a format other than a byte format, or has been released; each
    refusal reads as unhashable, so the same tuple given twice is one value instead of
    an error."""
    view = make()
    assert len(fields._validate_listable_field(None, [(view,), (view,)])) == 1


def test_a_memoryview_content_key_follows_the_shape_and_values_it_compares_by():
    """A view equals bytes holding the same values when it is one-dimensional, whatever
    its format, so it then has the content key those bytes have; a view of another shape
    or of other values has another key, and a released view, which equals only itself,
    is keyed without being read."""
    from lionagi.ln import hash_dict

    _, short_view, _ = _byte_views()
    assert hash_dict({"v": short_view}) == hash_dict({"v": b"ab"})
    doubles = memoryview(array("d", [1.0, 2.0]).tobytes()).cast("d")
    assert doubles == b"\x01\x02"
    assert hash_dict({"v": doubles}) == hash_dict({"v": b"\x01\x02"})
    square, wide = memoryview(b"abcd").cast("B", (2, 2)), memoryview(b"abcd").cast("H")
    assert square != b"abcd" and wide != b"abcd"
    assert hash_dict({"v": square}) != hash_dict({"v": b"abcd"})
    assert hash_dict({"v": wide}) != hash_dict({"v": b"abcd"})
    released = _released_view()
    assert hash_dict({"v": released}) == hash_dict({"v": released})


class FailingSerializer(BaseModel):
    """A model whose own field serializer reports a defect with ``ValueError``."""

    value: int = 0

    @field_serializer("value")
    def _fail(self, value):
        raise ValueError("a broken serializer")


def test_a_model_serializer_error_propagates_instead_of_reading_as_unhashable():
    """Only the serializer's refusal of a model that holds itself reads as an unhashable
    state; any other error from a model's own serializer is a defect in it and propagates
    from the base equality and from the content key."""
    from lionagi.ln import hash_dict

    with pytest.raises(ValueError, match="a broken serializer"):
        _ = MutableContext([FailingSerializer()]) == MutableContext([FailingSerializer()])
    with pytest.raises(ValueError, match="a broken serializer"):
        hash_dict({"v": FailingSerializer()})


def _view_pairs():
    """Read-only views of ``bytes`` that compare equal across formats, and views that
    hold equal values in another shape or other values and so compare unequal."""
    doubles = memoryview(array("d", [1.0, 2.0]).tobytes()).cast("d")
    corners = b"\x01\x00\x01\x01"
    return (
        (doubles, memoryview(b"\x01\x02")),
        (memoryview(b"\x01\x00").cast("?"), memoryview(b"\x01\x00")),
        (
            memoryview(array("f", [1.5]).tobytes()).cast("f"),
            memoryview(array("d", [1.5]).tobytes()).cast("d"),
        ),
        (memoryview(corners).cast("?", (2, 2)), memoryview(corners).cast("B", (2, 2))),
        (memoryview(corners), memoryview(corners).cast("B", (1, 4))),
        (memoryview(corners).cast("B", (2, 2)), memoryview(b"\x01\x01\x01\x00").cast("B", (2, 2))),
        (memoryview(corners).cast("B", (2, 2)), memoryview(b"\x01\x00\x01\x01")),
        (
            memoryview(b"\x01\x00\x00\x01").cast("B", (2, 2)),
            memoryview(array("d", [1.0, 0.0, 0.0, 1.0]).tobytes()).cast("d", (2, 2)),
        ),
        (
            memoryview(array("i", [256]).tobytes()).cast("i"),
            memoryview(array("d", [256.0]).tobytes()).cast("d"),
        ),
        (memoryview(b"\x01").cast("B", ()), memoryview(b"\x01").cast("B", ())),
        (memoryview(b"\x01").cast("B", ()), memoryview(b"\x02").cast("B", ())),
        (
            memoryview(b"\x01").cast("B", ()),
            memoryview(array("d", [1.0]).tobytes()).cast("d", ()),
        ),
    )


@pytest.mark.parametrize(
    ("left", "right"),
    _view_pairs(),
    ids=[
        "doubles-vs-bytes",
        "bool-vs-byte",
        "float-vs-double",
        "bool-corners-vs-bytes",
        "flat-vs-row",
        "reordered-corners",
        "corners-vs-flat-bytes",
        "byte-grid-vs-whole-double-grid",
        "int-vs-whole-double-past-a-byte",
        "zero-dim-equal",
        "zero-dim-unequal",
        "zero-dim-byte-vs-double",
    ],
)
def test_a_memoryview_field_follows_memoryview_equality(left, right):
    """A view compares equal to every view of the same shape holding equal values,
    whatever their formats, and to the bytes it holds when one-dimensional, so a value
    holding it has the same equality and hash as one holding any view it equals. Views
    that compare unequal compare unequal, have other hashes, and are two values under
    deduplication."""
    from lionagi.ln import hash_dict

    if left != right:
        assert Meta("k", left) != Meta("k", right)
        assert hash(Meta("k", left)) != hash(Meta("k", right))
        assert hash_dict({"v": left}) != hash_dict({"v": right})
        assert len(to_list([Meta("k", left), Meta("k", right)], flatten=True, unique=True)) == 2
        return
    assert Meta("k", left) == Meta("k", right)
    assert hash(Meta("k", left)) == hash(Meta("k", right))
    assert hash_dict({"v": left}) == hash_dict({"v": right})
    assert MutableContext([left]) == MutableContext([right])
    assert len(to_list([Meta("k", left), Meta("k", right)], flatten=True, unique=True)) == 1
    assert (
        len(
            to_list(
                [MutableContext([left]), MutableContext([right])],
                flatten=True,
                unique=True,
            )
        )
        == 1
    )


def test_a_released_view_is_not_answered_from_its_earlier_key():
    """A one-dimensional byte view keys as the bytes it holds, and that key is never
    cached: once the view is released it equals only itself, so a holder hashed before
    the release refuses afterwards, is no longer one value with a live equal view, and
    is refused by deduplication as a holder of a writable view is, instead of being
    dropped as a duplicate of the live view."""
    view = memoryview(b"ab")
    holder = Meta("k", view)
    hash(holder)
    view.release()
    with pytest.raises(TypeError):
        hash(holder)
    assert holder != Meta("k", memoryview(b"ab"))
    with pytest.raises(ValueError, match="Unhashable"):
        to_list([holder, Meta("k", memoryview(b"ab"))], flatten=True, unique=True)
    writable = Meta("k", memoryview(bytearray(b"ab")))
    with pytest.raises(ValueError, match="Unhashable"):
        to_list([writable, Meta("k", memoryview(b"ab"))], flatten=True, unique=True)


def test_a_model_serializer_error_propagates_from_deduplication():
    """Deduplicating models takes a content key, and a field serializer's ``ValueError``
    is a defect in the model, not an unhashable value: it propagates instead of leaving
    the models in the keyless bucket to be compared only by equality."""
    with pytest.raises(ValueError, match="a broken serializer"):
        to_list([FailingSerializer(), FailingSerializer()], flatten=True, unique=True)
    with pytest.raises(ValueError, match="a broken serializer"):
        to_list([{"v": FailingSerializer()}, {"v": FailingSerializer()}], flatten=True, unique=True)


def test_a_model_serializer_error_propagates_from_the_stable_cache_key():
    """A model whose serializer reports a defect is not a value whose key says it is
    unfit for caching; the ``ValueError`` propagates rather than becoming a refusal."""
    from lionagi.ln import _structural

    with pytest.raises(ValueError, match="a broken serializer"):
        _structural._try_stable_cache_key(FailingSerializer())


@pytest.mark.parametrize("kind", ["mapping", "pydantic", "msgspec"])
@pytest.mark.parametrize("collide", [False, True])
def test_generic_model_buckets_retain_hash_partition_and_equality_confirmation(
    monkeypatch, kind, collide
):
    comparisons = []

    class Payload(BaseModel):
        value: int

        def __eq__(self, other):
            comparisons.append(other)
            return super().__eq__(other)

    if kind == "msgspec":
        msgspec = pytest.importorskip("msgspec")

        class PayloadStruct(msgspec.Struct):
            value: int

        factory = lambda value: PayloadStruct(value)
    elif kind == "pydantic":
        factory = lambda value: Payload(value=value)
    else:
        factory = lambda value: {"value": value}

    calls = []

    def bucket(value):
        calls.append(value)
        return 0 if collide else (value["value"] if kind == "mapping" else value.value)

    monkeypatch.setattr(import_module("lionagi.ln._to_list"), "hash_dict", bucket)
    values = [factory(i) for i in range(20)]
    duplicates = [factory(i) for i in range(20)]
    result = to_list(values + duplicates, flatten=True, unique=True)
    assert len(calls) == 40
    assert len(result) == 20
    assert all(actual is expected for actual, expected in zip(result, values))
    if kind == "pydantic" and not collide:
        assert len(comparisons) == 20


@pytest.mark.parametrize(
    ("model", "attribute"),
    [
        (fields.Instruct, "instruction"),
        (fields.Reason, "title"),
        (fields.ActionRequestModel, "function"),
        (fields.ActionResponseModel, "output"),
    ],
)
def test_operation_dedup_does_not_hash_models(monkeypatch, model, attribute):
    monkeypatch.setattr(HashableModel, "__hash__", _unexpected_hash)
    monkeypatch.setattr(import_module("lionagi.ln._to_list"), "hash_dict", _unexpected_hash)
    first, duplicate, distinct = (model(**{attribute: value}) for value in ("a", "a", "b"))
    result = fields._validate_listable_field(None, [first, [None, duplicate, distinct], first])
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is distinct


class OpaqueOutput:
    __hash__ = None

    def __init__(self, value, representation):
        self.value = value
        self.representation = representation

    def __eq__(self, other):
        return isinstance(other, OpaqueOutput) and self.value == other.value

    def __repr__(self):
        return self.representation


def test_operation_dedup_honors_opaque_equality_instead_of_repr():
    first = fields.ActionResponseModel(output=OpaqueOutput(1, "first"))
    equal = fields.ActionResponseModel(output=OpaqueOutput(1, "different representation"))
    unequal = fields.ActionResponseModel(output=OpaqueOutput(2, "first"))
    assert first == equal
    assert first != unequal
    result = fields._validate_listable_field(None, [first, equal, unequal, first])
    assert len(result) == 2
    assert result[0] is first
    assert result[1] is unequal


def _array(values):
    return pytest.importorskip("numpy").array(values)


def _frame(values):
    return pytest.importorskip("pandas").DataFrame({"x": values})


@pytest.mark.parametrize("make", [_array, _frame], ids=["array", "frame"])
def test_operation_dedup_keeps_outputs_whose_equality_has_no_single_answer(make):
    """An array or frame output compares element by element, so asking its ``==``
    for one truth value raises; the responses stay separate values instead."""
    first = fields.ActionResponseModel(output=make([1, 2]))
    same_values = fields.ActionResponseModel(output=make([1, 2]))
    other = fields.ActionResponseModel(output=make([3, 4]))
    result = fields._validate_listable_field(None, [first, same_values, other, first])
    assert len(result) == 3
    assert result[0] is first
    assert result[1] is same_values
    assert result[2] is other


def test_dataclass_dedup_compares_only_values_with_the_same_structure(monkeypatch):
    """Distinct values are told apart by their structure before any comparison,
    so deduplicating a long list does not compare every pair."""
    calls = []
    compare = DataClass.__eq__

    def counted(self, other):
        calls.append(other)
        return compare(self, other)

    monkeypatch.setattr(DataClass, "__eq__", counted)
    values = [MutableContext([n]) for n in range(200)]
    repeat = MutableContext([7])
    result = to_list([values, repeat], flatten=True, unique=True)
    assert len(result) == 200
    assert all(kept is value for kept, value in zip(result, values, strict=True))
    assert len(calls) == 1
    assert calls[0] is repeat


def test_dataclass_dedup_tells_distinct_values_apart_by_key_without_comparing(monkeypatch):
    """Distinct base-equality values that can be keyed are told apart by their keys, so
    deduplicating them compares nothing: the work is linear in the values that can be
    keyed."""
    calls = []
    compare = DataClass.__eq__

    def counted(self, other):
        calls.append(other)
        return compare(self, other)

    monkeypatch.setattr(DataClass, "__eq__", counted)
    values = [MutableContext([n]) for n in range(2000)]
    result = to_list([values], flatten=True, unique=True)
    assert len(result) == 2000
    assert calls == []


class CountedKey:
    """Keyable, so a distinct value is told apart by its key; its equality counts."""

    calls: list = []

    def __init__(self, value):
        self.value = value

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        CountedKey.calls.append(other)
        return isinstance(other, CountedKey) and self.value == other.value


def test_operation_dedup_tells_distinct_keyable_values_apart_by_key_without_comparing():
    """An all-keyable operation input is deduplicated by key in one pass, so it compares
    nothing and stays linear."""
    CountedKey.calls.clear()
    values = [CountedKey(n) for n in range(2000)]
    result = fields._validate_listable_field(None, values)
    assert len(result) == 2000
    assert CountedKey.calls == []


class CountedKeyless:
    """Without a key, so each value is compared pairwise against the kept values."""

    calls: list = []

    __hash__ = None

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        CountedKeyless.calls.append(other)
        return isinstance(other, CountedKeyless) and self.value == other.value


def test_operation_dedup_compares_a_keyless_value_against_the_kept_values():
    """A value without a key is compared pairwise against the kept values, and each pair
    is asked in both directions, so six distinct values compare 30 times: about u times
    the kept count, doubled by the two directions."""
    CountedKeyless.calls.clear()
    values = [CountedKeyless(n) for n in range(6)]
    result = fields._validate_listable_field(None, values)
    assert len(result) == 6
    assert len(CountedKeyless.calls) == 30


def test_operation_dedup_keeps_requests_whose_arguments_have_no_single_answer():
    numpy = pytest.importorskip("numpy")
    first = fields.ActionRequestModel(function="f", arguments={"x": numpy.array([1, 2])})
    same_values = fields.ActionRequestModel(function="f", arguments={"x": numpy.array([1, 2])})
    scalar = fields.ActionRequestModel(function="f", arguments={"x": 1})
    scalar_again = fields.ActionRequestModel(function="f", arguments={"x": 1})
    result = fields._validate_listable_field(
        None, [first, same_values, scalar, scalar_again, first]
    )
    assert len(result) == 3
    assert result[0] is first
    assert result[1] is same_values
    assert result[2] is scalar


def test_operation_dedup_checks_identity_before_equality(monkeypatch):
    first = fields.ActionResponseModel(output=OpaqueOutput(1, "opaque"))

    def unexpected_equality(_self, _other):
        raise AssertionError("Repeated instances do not require value equality")

    monkeypatch.setattr(fields.ActionResponseModel, "__eq__", unexpected_equality)
    assert fields._validate_listable_field(None, [first, first])[0] is first


def test_generic_unrelated_unhashable_still_raises():
    with pytest.raises(ValueError, match="Unhashable type encountered"):
        to_list([OpaqueOutput(1, "opaque")], flatten=True, unique=True)


@pytest.mark.parametrize("name", tuple(fields._FIELD_CONSTANTS))
def test_all_lazy_field_constants_preserve_identity(name):
    assert getattr(fields, name) is getattr(fields, name)


def test_arbitrary_default_fields_are_fresh_without_copying_the_default():
    default = OpaqueOutput(1, "default")
    first = fields.get_default_field("reason", default=default, nullable=False)
    second = fields.get_default_field("reason", default=default, nullable=False)
    assert first is not second
    assert first.extract_metadata("default") is default
    assert second.extract_metadata("default") is default


@pytest.mark.parametrize(
    "value",
    [
        fields.Instruct(instruction="work"),
        fields.Reason(title="reason"),
        fields.ActionRequestModel(function="f", arguments={"n": 1}),
        fields.ActionResponseModel(function="f", output={"n": 1}),
        ReActAnalysis(analysis="continue", extension_needed=True),
        Analysis(answer="done"),
    ],
)
def test_mutable_dto_roundtrips_and_log_projection(value):
    from lionagi.protocols.generic.log import Log

    restored = type(value).from_json(value.to_json())
    assert restored == value
    assert restored is not value
    assert Log.create(value).content == value.to_dict(mode="json")


def test_function_schema_shape_and_mutation_remain_available():
    schema = FunctionSchema(name="f", parameters={"type": "object"})
    assert schema.to_dict() == {
        "type": "function",
        "function": {
            "name": "f",
            "description": None,
            "parameters": {"type": "object"},
            "strict": None,
        },
    }
    schema.name = "updated"
    assert schema.to_dict()["function"]["name"] == "updated"


def test_immutable_configuration_hashes_and_equality_are_unchanged():
    @dataclass(slots=True, frozen=True, init=False, eq=False)
    class Options(Params):
        value: tuple[int, ...] = (1, 2)

    for first, equal in [
        (Options(), Options()),
        (Meta("x", (1, 2)), Meta("x", (1, 2))),
        (Spec(str, name="value"), Spec(str, name="value")),
    ]:
        assert first == equal
        assert hash(first) == hash(equal)
        assert equal in {first}
        assert {first: "present"}[equal] == "present"
