# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mutable model admission, ordered deduplication, and compatibility contracts."""

from __future__ import annotations

import json
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
