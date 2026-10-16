# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mutable model admission, ordered deduplication, and compatibility contracts."""

from __future__ import annotations

import json
import operator
import sys
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import BaseModel

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
class Strict(DataClass):
    """Its own equality, without a hash, reads a field the other value may not carry."""

    k: int = 0

    def __eq__(self, other):
        return self.k == other.k

    __hash__ = None


@pytest.mark.parametrize("keyed_first", [False, True], ids=["keyed-first", "plain-first"])
def test_dataclass_dedup_lets_a_hashable_own_equality_error_propagate_in_either_order(
    keyed_first,
):
    """An error other than ``TypeError`` or ``ValueError`` is not a comparison result."""

    keyed, plain = Keyed(1), Point(1)
    first, second = (keyed, plain) if keyed_first else (plain, keyed)
    with pytest.raises(AttributeError):
        to_list([first, second], flatten=True, unique=True)


@pytest.mark.parametrize("strict_first", [False, True], ids=["strict-first", "plain-first"])
def test_dataclass_dedup_lets_an_unhashable_own_equality_error_propagate_in_either_order(
    strict_first,
):
    strict, plain = Strict(1), Point(1)
    first, second = (strict, plain) if strict_first else (plain, strict)
    with pytest.raises(AttributeError):
        to_list([first, second], flatten=True, unique=True)


@dataclass(slots=True, eq=False)
class Incomparable(DataClass):
    """Its own equality refuses a pair it cannot compare, with ``ValueError``."""

    n: int = 0

    def __eq__(self, other):
        raise ValueError("incomparable values")

    __hash__ = None


@pytest.mark.parametrize("refusing_first", [False, True], ids=["refusing-first", "plain-first"])
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


class Pe:
    __hash__ = None

    def __eq__(self, other):
        return False


class Qe:
    __hash__ = None

    def __eq__(self, other):
        return isinstance(other, Pe)


@pytest.mark.parametrize("pe_first", [False, True], ids=["pe-first", "qe-first"])
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
def test_dataclass_dedup_keeps_a_value_whose_structure_cannot_be_keyed(make):
    """DataClass equality cannot compare such a value either, so it is kept unless it
    is the same object as a kept value, and the values around it are deduplicated."""
    first, equal_content, plain = Holder(make()), Holder(make()), Holder("plain")
    with pytest.raises((ValueError, RecursionError)):
        operator.eq(first, equal_content)
    result = to_list(
        [first, plain, equal_content, first, Holder("plain")], flatten=True, unique=True
    )
    assert len(result) == 3
    assert result[0] is first
    assert result[1] is plain
    assert result[2] is equal_content


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
