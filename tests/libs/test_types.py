"""Tests for lionagi/ln/types.py"""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from lionagi.ln.types import (
    DataClass,
    Enum,
    ModelConfig,
    Params,
    Undefined,
    Unset,
    is_sentinel,
    not_sentinel,
)


class MyTestEnum(Enum):
    """Test enum for testing"""

    VALUE1 = "value1"
    VALUE2 = "value2"
    VALUE3 = "value3"


def test_enum_allowed():
    """Test Enum.allowed() method - Line 154"""
    allowed = MyTestEnum.allowed()
    assert isinstance(allowed, tuple)
    assert "value1" in allowed
    assert "value2" in allowed
    assert "value3" in allowed
    assert len(allowed) == 3


@dataclass(slots=True, frozen=True, init=False)
class MyParams(Params):
    """Test params class"""

    field1: str = Unset
    field2: int = Unset
    field3: bool = Unset


@dataclass(slots=True, frozen=True, init=False)
class ParamsWithDefaults(Params):
    """Params fixture covering dataclass default semantics."""

    label: str = "default-label"
    items: list[str] = field(default_factory=list)


def test_params_invalid_parameter():
    """Test Params.__init__ with invalid parameter - Line 188"""
    with pytest.raises(ValueError, match="Invalid parameter"):
        MyParams(field1="valid", invalid_field="should fail")


def test_params_valid():
    """Test Params.__init__ with valid parameters"""
    params = MyParams(field1="test", field2=42)
    assert params.field1 == "test"
    assert params.field2 == 42


def test_params_uses_declared_dataclass_default():
    params = ParamsWithDefaults()

    assert params.label == "default-label"


def test_params_explicit_value_overrides_declared_default():
    params = ParamsWithDefaults(label="explicit")

    assert params.label == "explicit"


def test_params_calls_default_factory_for_each_instance():
    calls = 0

    def make_items() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    @dataclass(slots=True, frozen=True, init=False)
    class CountingParams(Params):
        items: list[str] = field(default_factory=make_items)

    first = CountingParams()
    second = CountingParams()

    assert first.items == []
    assert second.items == []
    assert first.items is not second.items
    assert calls == 2


def test_params_explicit_value_skips_default_factory():
    calls = 0

    def make_items() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    @dataclass(slots=True, frozen=True, init=False)
    class CountingParams(Params):
        items: list[str] = field(default_factory=make_items)

    supplied = ["explicit"]
    params = CountingParams(items=supplied)

    assert params.items is supplied
    assert calls == 0


def test_params_rejects_unknown_key_before_calling_default_factory():
    calls = 0

    def make_items() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    @dataclass(slots=True, frozen=True, init=False)
    class CountingParams(Params):
        items: list[str] = field(default_factory=make_items)

    with pytest.raises(ValueError, match="Invalid parameter"):
        CountingParams(unknown=True)

    assert calls == 0


def test_params_allowed():
    """Test Params.allowed() method"""
    allowed = MyParams.allowed()
    assert isinstance(allowed, frozenset)
    assert "field1" in allowed
    assert "field2" in allowed
    assert "field3" in allowed
    assert "_none_as_sentinel" not in allowed  # Private fields excluded


@dataclass(slots=True, frozen=True, init=False)
class UnauthorizedParamsNoneSentinel(Params):
    """A new core config that is not a named compatibility adapter."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    field1: str = Unset


def test_unlisted_params_cannot_enable_none_collapse():
    with pytest.raises(ValueError, match="not allowlisted"):
        UnauthorizedParamsNoneSentinel._is_sentinel(None)


def test_params_is_sentinel_default():
    """Test Params._is_sentinel with default (_none_as_sentinel=False)"""
    # When _none_as_sentinel is False, None is not a sentinel
    assert MyParams._is_sentinel(None) is False
    assert MyParams._is_sentinel(Undefined) is True
    assert MyParams._is_sentinel(Unset) is True
    assert MyParams._is_sentinel("value") is False


@dataclass(slots=True, frozen=True, init=False)
class MyParamsStrict(Params):
    """Test params class with strict mode"""

    _config: ClassVar[ModelConfig] = ModelConfig(strict=True)
    field1: str = Unset
    field2: int = Unset


def test_params_strict_mode():
    """Test Params strict mode validation - Lines 246-248"""
    with pytest.raises(ValueError, match="Missing required parameter"):
        MyParamsStrict(field1="value")  # field2 is missing and strict=True


@dataclass(slots=True)
class MyDataClass(DataClass):
    """Test data class"""

    field1: str = Unset
    field2: int = Unset


def test_dataclass_valid():
    """Test DataClass with valid fields"""
    obj = MyDataClass(field1="test", field2=42)
    assert obj.field1 == "test"
    assert obj.field2 == 42


def test_dataclass_allowed():
    """Test DataClass.allowed() method - Line 214"""
    allowed = MyDataClass.allowed()
    assert isinstance(allowed, frozenset)
    assert "field1" in allowed
    assert "field2" in allowed


@dataclass(slots=True)
class MyDataClassStrict(DataClass):
    """Test data class with strict mode"""

    _config: ClassVar[ModelConfig] = ModelConfig(strict=True)
    field1: str = Unset


def test_dataclass_strict_mode():
    """Test DataClass strict mode - Lines 246-248"""
    with pytest.raises(ValueError, match="Missing required parameter"):
        MyDataClassStrict()  # Missing required field in strict mode


@dataclass(slots=True)
class MyDataClassPrefillUnset(DataClass):
    """Test data class with prefill_unset"""

    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=True)
    field1: str = field(default=Undefined)


def test_dataclass_prefill_unset():
    """Test DataClass prefill_unset behavior - Lines 251-253"""
    obj = MyDataClassPrefillUnset()
    # Field initialized to Undefined should be prefilled with Unset
    assert obj.field1 is Unset


@dataclass(slots=True)
class UnauthorizedDataClassNoneSentinel(DataClass):
    """A new mutable config that is not a named compatibility adapter."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    field1: str = None


def test_unlisted_dataclass_cannot_enable_none_collapse():
    with pytest.raises(ValueError, match="not allowlisted"):
        UnauthorizedDataClassNoneSentinel._is_sentinel(None)


def test_dataclass_to_dict():
    """Test DataClass.to_dict() method"""
    obj = MyDataClass(field1="test", field2=42)
    result = obj.to_dict()
    assert "field1" in result
    assert "field2" in result


def test_dataclass_to_dict_exclude():
    """Test DataClass.to_dict() with exclude"""
    obj = MyDataClass(field1="test", field2=42)
    result = obj.to_dict(exclude={"field2"})
    assert "field1" in result
    assert "field2" not in result


def test_dataclass_with_updates():
    """Test DataClass.with_updates() method"""
    obj = MyDataClass(field1="test", field2=42)
    updated = obj.with_updates(field2=100)
    assert updated.field1 == "test"
    assert updated.field2 == 100


def test_dataclass_hash():
    """Test DataClass.__hash__() method"""
    # DataClass needs to be frozen to be hashable, use Params instead
    params1 = MyParams(field1="test", field2=42)
    params2 = MyParams(field1="test", field2=42)
    hash1 = hash(params1)
    hash2 = hash(params2)
    assert isinstance(hash1, int)
    assert isinstance(hash2, int)


def test_dataclass_eq():
    """Test DataClass.__eq__() method"""
    obj1 = MyDataClass(field1="test", field2=42)
    obj2 = MyDataClass(field1="test", field2=42)
    obj3 = MyDataClass(field1="other", field2=99)
    assert obj1 == obj2
    assert obj1 != obj3


def test_dataclass_eq_not_dataclass():
    """Test DataClass.__eq__() with non-DataClass"""
    obj = MyDataClass(field1="test", field2=42)
    assert obj != "not a dataclass"
    assert obj != 42


def test_params_to_dict():
    """Test Params.to_dict() method"""
    params = MyParams(field1="test", field2=42)
    result = params.to_dict()
    assert "field1" in result
    assert "field2" in result


def test_params_to_dict_exclude():
    """Test Params.to_dict() with exclude"""
    params = MyParams(field1="test", field2=42)
    result = params.to_dict(exclude={"field2"})
    assert "field1" in result
    assert "field2" not in result


def test_params_with_updates():
    """Test Params.with_updates() method"""
    params = MyParams(field1="test", field2=42)
    updated = params.with_updates(field2=100)
    assert updated.field1 == "test"
    assert updated.field2 == 100


def test_params_hash():
    """Test Params.__hash__() method"""
    params1 = MyParams(field1="test", field2=42)
    params2 = MyParams(field1="test", field2=42)
    # Just verify hash can be computed
    hash1 = hash(params1)
    hash2 = hash(params2)
    assert isinstance(hash1, int)
    assert isinstance(hash2, int)


def test_params_eq():
    """Test Params.__eq__() method"""
    params1 = MyParams(field1="test", field2=42)
    params2 = MyParams(field1="test", field2=42)
    params3 = MyParams(field1="other", field2=99)
    assert params1 == params2
    assert params1 != params3


def test_params_eq_not_params():
    """Test Params.__eq__() with non-Params"""
    params = MyParams(field1="test", field2=42)
    assert params != "not params"
    assert params != 42


def test_params_default_kw():
    """Test Params.default_kw() method"""
    params = MyParams(field1="test", field2=42)
    result = params.default_kw()
    assert isinstance(result, dict)
    assert result["field1"] == "test"
    assert result["field2"] == 42


@dataclass(slots=True, frozen=True, init=False)
class InheritedParamsBase(Params):
    inherited: str = "base"
    class_only: ClassVar[str] = "not-a-field"


@dataclass(slots=True, frozen=True, init=False)
class InheritedParams(InheritedParamsBase):
    own: int = 1
    items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class InheritedDataClassBase(DataClass):
    inherited: str = "base"
    class_only: ClassVar[str] = "not-a-field"


@dataclass(slots=True)
class InheritedDataClass(InheritedDataClassBase):
    own: int = 1
    items: list[str] = field(default_factory=list)


def test_params_and_dataclass_share_ordered_field_discovery():
    expected = ("inherited", "own", "items")

    assert InheritedParams.field_names() == expected
    assert InheritedDataClass.field_names() == expected
    assert tuple(InheritedParams().to_dict()) == expected
    assert tuple(InheritedDataClass().to_dict()) == expected
    assert "class_only" not in InheritedParams.allowed()
    assert "class_only" not in InheritedDataClass.allowed()


def test_params_rejects_public_classvar_as_constructor_input():
    with pytest.raises(ValueError, match="Invalid parameter: class_only"):
        InheritedParams(class_only="not-instance-state")


def test_allowed_is_immutable_membership_view():
    assert InheritedParams.allowed() == frozenset({"inherited", "own", "items"})
    assert InheritedDataClass.allowed() == frozenset({"inherited", "own", "items"})

    with pytest.raises(AttributeError):
        InheritedParams.allowed().add("injected")  # type: ignore[attr-defined]


def test_field_discovery_does_not_run_factories():
    calls = 0

    def make_value() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    @dataclass(slots=True, frozen=True, init=False)
    class FactoryParams(Params):
        value: list[str] = field(default_factory=make_value)

    assert FactoryParams.field_names() == ("value",)
    assert FactoryParams.allowed() == frozenset({"value"})
    assert calls == 0


@dataclass(slots=True, frozen=True, init=False)
class AbsenceParams(Params):
    missing: object
    unresolved: object = Unset
    explicit_null: object = None
    false_value: bool = False
    zero_value: int = 0
    empty_string: str = ""
    empty_list: list[object] = field(default_factory=list)
    empty_dict: dict[str, object] = field(default_factory=dict)


def test_wire_projection_preserves_three_state_absence_and_falsy_values():
    value = AbsenceParams()

    assert value.missing is Unset
    assert value.unresolved is Unset
    assert value.to_dict() == {
        "explicit_null": None,
        "false_value": False,
        "zero_value": 0,
        "empty_string": "",
        "empty_list": [],
        "empty_dict": {},
    }


def test_constructor_preserves_explicit_null_instead_of_unset():
    @dataclass(slots=True, frozen=True, init=False)
    class NullableParams(Params):
        value: object

    missing = NullableParams()
    explicit = NullableParams(value=None)

    assert missing.value is Unset
    assert explicit.value is None
    assert "value" not in missing.to_dict()
    assert explicit.to_dict()["value"] is None


@dataclass(slots=True, frozen=True, init=False)
class NoPrefillParams(Params):
    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=False)
    missing: object


def test_missing_field_can_remain_undefined_without_prefill():
    value = NoPrefillParams()

    assert value.missing is Undefined
    assert "missing=Undefined" in repr(value)
    assert value.to_dict() == {}

    updated = value.with_updates()
    assert updated.missing is Undefined


def test_strict_validation_reports_first_missing_field_in_declaration_order():
    @dataclass(slots=True, frozen=True, init=False)
    class OrderedStrictParams(Params):
        _config: ClassVar[ModelConfig] = ModelConfig(strict=True)
        alpha: object
        beta: object

    with pytest.raises(ValueError, match="Missing required parameter: alpha"):
        OrderedStrictParams()

    @dataclass(slots=True)
    class OrderedStrictDataClass(DataClass):
        _config: ClassVar[ModelConfig] = ModelConfig(strict=True)
        alpha: object = Undefined
        beta: object = Undefined

    with pytest.raises(ValueError, match="Missing required parameter: alpha"):
        OrderedStrictDataClass()


@dataclass(slots=True)
class AbsenceDataClass(DataClass):
    missing: object = Undefined
    unresolved: object = Unset
    explicit_null: object = None
    false_value: bool = False
    zero_value: int = 0
    empty_string: str = ""
    empty_list: list[object] = field(default_factory=list)
    empty_dict: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class NoPrefillDataClass(DataClass):
    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=False)
    missing: object = Undefined


def test_dataclass_wire_projection_matches_params_absence_rules():
    value = AbsenceDataClass()

    assert value.missing is Unset
    assert value.unresolved is Unset
    assert value.to_dict() == {
        "explicit_null": None,
        "false_value": False,
        "zero_value": 0,
        "empty_string": "",
        "empty_list": [],
        "empty_dict": {},
    }


def test_dataclass_update_preserves_undefined_without_prefill():
    value = NoPrefillDataClass()

    updated = value.with_updates()

    assert value.missing is Undefined
    assert updated.missing is Undefined


def test_dataclass_unknown_key_fails_before_default_factory():
    calls = 0

    def make_items() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    @dataclass(slots=True)
    class CountingDataClass(DataClass):
        items: list[str] = field(default_factory=make_items)

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        CountingDataClass(unknown=True)  # type: ignore[call-arg]

    assert calls == 0


def test_exclude_is_membership_only_and_preserves_declaration_order():
    expected = ("inherited", "items")

    assert tuple(InheritedParams().to_dict(exclude={"own"})) == expected
    assert tuple(InheritedDataClass().to_dict(exclude={"own"})) == expected


@dataclass(slots=True, frozen=True, init=False)
class StatefulParams(Params):
    value: object = "declared-default"
    other: int = 1


@dataclass(slots=True)
class StatefulDataClass(DataClass):
    value: object = "declared-default"
    other: int = 1


@dataclass(slots=True)
class DerivedDataClass(DataClass):
    value: int = 1
    derived: int = field(default=2, init=False)


@dataclass(slots=True)
class ValidatedDerivedDataClass(DataClass):
    value: int = 1
    derived: int = field(default=1, init=False)

    def __post_init__(self):
        self.derived = self.value
        DataClass.__post_init__(self)

    def _validate(self):
        DataClass._validate(self)
        if self.value != self.derived:
            raise ValueError("derived must match value")


@pytest.mark.parametrize("model_type", [StatefulParams, StatefulDataClass])
def test_with_updates_preserves_unset_in_memory_state(model_type):
    original = model_type(value=Unset)

    updated = original.with_updates(other=2)

    assert updated.value is Unset
    assert updated.other == 2


@pytest.mark.parametrize("model_type", [StatefulParams, StatefulDataClass])
def test_with_updates_preserves_explicit_null(model_type):
    original = model_type(value=None)

    updated = original.with_updates(other=2)

    assert updated.value is None
    assert updated.other == 2


def test_dataclass_with_updates_preserves_and_can_change_public_init_false_state():
    original = DerivedDataClass(value=1)
    original.derived = 7

    preserved = original.with_updates(value=2)
    changed = original.with_updates(value=3, derived=11)

    assert (preserved.value, preserved.derived) == (2, 7)
    assert (changed.value, changed.derived) == (3, 11)


def test_dataclass_with_updates_revalidates_restored_init_false_state():
    original = ValidatedDerivedDataClass(value=1)

    with pytest.raises(ValueError, match="derived must match value"):
        original.with_updates(value=2)

    valid = original.with_updates(value=2, derived=2)
    assert (valid.value, valid.derived) == (2, 2)


def test_with_updates_does_not_rerun_factory_for_unset_value():
    calls = 0

    def make_unset():
        nonlocal calls
        calls += 1
        return Unset

    @dataclass(slots=True, frozen=True, init=False)
    class FactoryParams(Params):
        value: object = field(default_factory=make_unset)
        other: int = 1

    original = FactoryParams()
    updated = original.with_updates(other=2)

    assert calls == 1
    assert updated.value is Unset


def test_declared_field_serialization_is_stable_across_hash_seeds():
    script = """
from dataclasses import dataclass
from typing import ClassVar
from lionagi.ln.types import DataClass, Params

@dataclass(slots=True, frozen=True, init=False)
class PBase(Params):
    alpha: str = "a"
    public_class_var: ClassVar[str] = "not-a-field"

@dataclass(slots=True, frozen=True, init=False)
class P(PBase):
    beta: str = "b"
    gamma: str = "c"

@dataclass(slots=True)
class DBase(DataClass):
    alpha: str = "a"
    public_class_var: ClassVar[str] = "not-a-field"

@dataclass(slots=True)
class D(DBase):
    beta: str = "b"
    gamma: str = "c"

print(",".join(P.field_names()))
print(",".join(P().to_dict()))
print(",".join(D.field_names()))
print(",".join(D().to_dict()))
"""
    repo_root = Path(__file__).parents[2]

    for seed in ("1", "7", "29", "101"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run(  # noqa: S603 - fixed interpreter and inline fixture
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.splitlines() == [
            "alpha,beta,gamma",
            "alpha,beta,gamma",
            "alpha,beta,gamma",
            "alpha,beta,gamma",
        ]


def test_is_sentinel():
    """Test is_sentinel function"""
    assert is_sentinel(Undefined) is True
    assert is_sentinel(Unset) is True
    assert is_sentinel(None) is False
    assert is_sentinel("value") is False
    assert is_sentinel(42) is False


def test_not_sentinel():
    """Test not_sentinel function"""
    assert not_sentinel(Undefined) is False
    assert not_sentinel(Unset) is False
    assert not_sentinel(None) is True
    assert not_sentinel("value") is True
    assert not_sentinel(42) is True
