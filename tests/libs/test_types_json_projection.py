"""JSON projection contract for the lightweight Params/DataClass substrate."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, field_serializer

from lionagi.ln.types import DataClass, Params, Spec, Unset


class ProjectionColor(Enum):
    BLUE = "blue"


class JsonAwareModel(BaseModel):
    value: int

    @field_serializer("value", when_used="json")
    def _serialize_value(self, value: int) -> str:
        return f"json:{value}"


@dataclass(slots=True)
class PlainChild:
    path: Path


@dataclass(slots=True, frozen=True, init=False)
class NestedParams(Params):
    value: str = "params"
    unresolved: object = Unset
    explicit_null: object = None
    falsey: dict[str, object] = field(
        default_factory=lambda: {
            "false": False,
            "zero": 0,
            "empty_string": "",
            "empty_list": [],
            "empty_dict": {},
        }
    )


@dataclass(slots=True)
class NestedDataClass(DataClass):
    value: str = "dataclass"
    unresolved: object = Unset
    explicit_null: object = None


@dataclass(slots=True, frozen=True, init=False)
class ProjectionParams(Params):
    params: NestedParams
    data_class: NestedDataClass
    nested_list: list[object]
    pydantic_model: JsonAwareModel
    plain_dataclass: PlainChild
    path: Path
    timestamp: dt.datetime
    color: ProjectionColor
    spec: Spec
    identifier: UUID
    decimal: Decimal
    date: dt.date
    time: dt.time


@dataclass(slots=True)
class ProjectionDataClass(DataClass):
    params: NestedParams
    payload: object


def _projection_params() -> ProjectionParams:
    nested_params = NestedParams()
    return ProjectionParams(
        params=nested_params,
        data_class=NestedDataClass(),
        nested_list=[nested_params],
        pydantic_model=JsonAwareModel(value=7),
        plain_dataclass=PlainChild(Path("artifacts/result.json")),
        path=Path("runs/current"),
        timestamp=dt.datetime(2026, 8, 16, 12, 30, 45),
        color=ProjectionColor.BLUE,
        spec=Spec(name="label"),
        identifier=UUID("12345678-1234-5678-1234-567812345678"),
        decimal=Decimal("12.50"),
        date=dt.date(2026, 8, 16),
        time=dt.time(12, 30, 45),
    )


def test_python_mode_preserves_native_nested_values():
    value = _projection_params()

    projected = value.to_dict()
    explicit = value.to_dict(mode="python")

    assert explicit == projected
    assert projected["params"] is value.params
    assert projected["data_class"] is value.data_class
    assert projected["pydantic_model"] is value.pydantic_model
    assert projected["plain_dataclass"] is value.plain_dataclass
    assert projected["path"] == Path("runs/current")
    assert projected["timestamp"] == dt.datetime(2026, 8, 16, 12, 30, 45)
    assert projected["color"] is ProjectionColor.BLUE
    assert projected["spec"] is value.spec
    assert projected["identifier"] == UUID("12345678-1234-5678-1234-567812345678")
    assert projected["decimal"] == Decimal("12.50")
    assert projected["date"] == dt.date(2026, 8, 16)
    assert projected["time"] == dt.time(12, 30, 45)


def test_mode_is_keyword_only_while_exclude_remains_positional():
    value = NestedParams()

    assert value.to_dict({"unresolved"}) == value.to_dict(exclude={"unresolved"})
    with pytest.raises(TypeError, match="positional"):
        value.to_dict(None, "json")  # type: ignore[call-arg]


def test_json_mode_recurses_through_the_internal_serializer():
    value = _projection_params()

    projected = value.to_dict(mode="json")

    assert tuple(projected) == value.field_names()
    assert projected["params"] == {
        "value": "params",
        "explicit_null": None,
        "falsey": {
            "false": False,
            "zero": 0,
            "empty_string": "",
            "empty_list": [],
            "empty_dict": {},
        },
    }
    assert projected["data_class"] == {
        "value": "dataclass",
        "explicit_null": None,
    }
    assert projected["nested_list"] == [
        {
            "value": "params",
            "explicit_null": None,
            "falsey": {
                "false": False,
                "zero": 0,
                "empty_string": "",
                "empty_list": [],
                "empty_dict": {},
            },
        }
    ]
    assert projected["pydantic_model"] == {"value": "json:7"}
    assert projected["plain_dataclass"] == {"path": "artifacts/result.json"}
    assert projected["path"] == "runs/current"
    assert projected["timestamp"] == "2026-08-16T12:30:45"
    assert projected["color"] == "blue"
    assert projected["spec"] == {"metadata": [{"key": "name", "value": "label"}]}
    assert projected["identifier"] == "12345678-1234-5678-1234-567812345678"
    assert projected["decimal"] == "12.50"
    assert projected["date"] == "2026-08-16"
    assert projected["time"] == "12:30:45"


def test_dataclass_json_mode_uses_the_same_projection_and_preserves_positional_exclude():
    value = ProjectionDataClass(
        params=NestedParams(),
        payload={"path": Path("payload/data.json")},
    )

    assert value.to_dict({"payload"}, mode="json") == {
        "params": {
            "value": "params",
            "explicit_null": None,
            "falsey": {
                "false": False,
                "zero": 0,
                "empty_string": "",
                "empty_list": [],
                "empty_dict": {},
            },
        }
    }
    assert value.to_dict(mode="json") == {
        "params": {
            "value": "params",
            "explicit_null": None,
            "falsey": {
                "false": False,
                "zero": 0,
                "empty_string": "",
                "empty_list": [],
                "empty_dict": {},
            },
        },
        "payload": {"path": "payload/data.json"},
    }


@pytest.mark.parametrize("model_type", [NestedParams, NestedDataClass])
def test_missing_and_explicit_null_remain_distinct_after_json_projection(model_type):
    missing = model_type(unresolved=Unset)
    explicit_null = model_type(unresolved=None)

    missing_payload = missing.to_dict(mode="json")
    null_payload = explicit_null.to_dict(mode="json")

    assert "unresolved" not in missing_payload
    assert null_payload["unresolved"] is None
    assert model_type(**missing_payload).unresolved is Unset
    assert model_type(**null_payload).unresolved is None


@pytest.mark.parametrize("mode", ["db", "yaml", "bogus"])
def test_lightweight_projection_rejects_unknown_modes(mode):
    with pytest.raises(ValueError, match="Unsupported serialization mode"):
        NestedParams().to_dict(mode=mode)  # type: ignore[arg-type]


def test_raw_nested_sentinel_fails_closed_instead_of_becoming_json_null():
    value = ProjectionDataClass(
        params=NestedParams(),
        payload={"user_values": [Unset]},
    )

    with pytest.raises(TypeError, match="UnsetType"):
        value.to_dict(mode="json")


def test_nested_serializer_exceptions_are_not_swallowed():
    class BrokenModel:
        def model_dump(self, *, mode: str):
            raise ValueError(f"broken in {mode}")

    value = ProjectionDataClass(params=NestedParams(), payload=BrokenModel())

    with pytest.raises(ValueError, match="broken in json"):
        value.to_dict(mode="json")


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_json_projection_rejects_non_finite_numbers_instead_of_writing_null(value):
    projected = ProjectionDataClass(
        params=NestedParams(),
        payload={"metric": value, "explicit_null": None},
    )

    with pytest.raises(ValueError, match=r"non-finite float at \$\.payload\.metric"):
        projected.to_dict(mode="json")


def test_json_projection_rejects_bytes_until_snapshot_encoding_is_versioned():
    value = ProjectionDataClass(params=NestedParams(), payload={"raw": b"opaque"})

    with pytest.raises(TypeError, match="bytes"):
        value.to_dict(mode="json")


def test_nested_model_owns_its_sentinel_policy():
    @dataclass(slots=True, frozen=True, init=False)
    class DomainParams(Params):
        value: object = "domain-missing"

        @classmethod
        def _is_sentinel(cls, value: object) -> bool:
            return value == "domain-missing" or Params._is_sentinel(value)

    outer = ProjectionDataClass(params=DomainParams(), payload={"keep": "value"})

    assert outer.to_dict(mode="json") == {
        "params": {},
        "payload": {"keep": "value"},
    }


def test_nested_domain_overrides_run_before_json_adaptation():
    from lionagi.casts.pattern import Role
    from lionagi.protocols.messages.instruction import InstructionContent

    value = ProjectionDataClass(
        params=NestedParams(),
        payload={
            "role": Role(name="reviewer", description="Review", emits=(str,)),
            "instruction": InstructionContent(
                instruction="Inspect",
                prompt_context=[Path("nested/context.json")],
            ),
        },
    )

    payload = value.to_dict(mode="json")["payload"]

    assert payload["role"] == {
        "name": "reviewer",
        "description": "Review",
        "emits": ["str"],
    }
    assert payload["instruction"]["prompt_context"] == ["nested/context.json"]


def test_one_root_projection_does_not_round_trip_each_nested_owner(monkeypatch):
    from lionagi.ln import _json_dump

    calls = 0
    original = _json_dump._to_json_value

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(_json_dump, "_to_json_value", counted)

    _projection_params().to_dict(mode="json")

    assert calls == 1


def test_plain_dataclass_keeps_field_projection_instead_of_claiming_substrate_semantics():
    @dataclass(slots=True)
    class PlainWithCustomMethod:
        path: Path

        def to_dict(self):
            raise AssertionError("plain dataclass method must not become substrate authority")

    value = ProjectionDataClass(
        params=NestedParams(),
        payload=PlainWithCustomMethod(Path("plain/value.json")),
    )

    assert value.to_dict(mode="json")["payload"] == {"path": "plain/value.json"}


def test_json_projection_rejects_cyclic_containers():
    payload: dict[str, object] = {}
    payload["self"] = payload
    value = ProjectionDataClass(params=NestedParams(), payload=payload)

    with pytest.raises(TypeError, match="Circular reference"):
        value.to_dict(mode="json")


def test_json_projection_rejects_model_returning_itself():
    class SelfReturningModel:
        def model_dump(self, *, mode: str):
            assert mode == "json"
            return self

    value = ProjectionDataClass(
        params=NestedParams(),
        payload=SelfReturningModel(),
    )

    with pytest.raises(TypeError, match="Circular reference"):
        value.to_dict(mode="json")


@dataclass(frozen=True)
class _OwnerToDictHolder(Params):
    inner: object = None


def test_a_nested_owner_keeps_its_own_to_dict_projection():
    """model_dump drops what an owner's to_dict adds, so the more specific one wins."""
    from lionagi.protocols.generic.element import Element

    element = Element()
    assert "lion_class" in element.to_dict()["metadata"], "control: to_dict carries it"
    assert "lion_class" not in element.model_dump(mode="json")["metadata"], (
        "control: model_dump drops it, which is what makes the ordering matter"
    )

    projected = _OwnerToDictHolder(inner=element).to_dict(mode="json")
    assert "lion_class" in projected["inner"]["metadata"], projected


def test_a_nested_model_omits_an_unset_rather_than_failing_to_serialize():
    from lionagi.models.note import Note

    projected = _OwnerToDictHolder(inner=Note(**{"a": Unset, "b": 1})).to_dict(mode="json")
    assert projected["inner"] == {"b": 1}, projected
