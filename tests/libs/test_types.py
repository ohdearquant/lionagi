"""Tests for lionagi/ln/types.py

Target: Cover easy missing lines (154, 188, 197, 214, etc.)
"""

from dataclasses import dataclass, field
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

# ============================================================================
# Test Enum.allowed() - Line 154
# ============================================================================


class MyTestEnum(Enum):
    """Test enum for testing"""

    VALUE1 = "value1"
    VALUE2 = "value2"
    VALUE3 = "value3"


def test_enum_allowed():
    allowed = MyTestEnum.allowed()
    assert isinstance(allowed, tuple)
    assert "value1" in allowed
    assert "value2" in allowed
    assert "value3" in allowed
    assert len(allowed) == 3


# ============================================================================
# Test Params - Lines 188, 197, 214
# ============================================================================


@dataclass(slots=True, frozen=True, init=False)
class MyParams(Params):
    """Test params class"""

    field1: str = Unset
    field2: int = Unset
    field3: bool = Unset


def test_params_invalid_parameter():
    with pytest.raises(ValueError, match="Invalid parameter"):
        MyParams(field1="valid", invalid_field="should fail")


def test_params_valid():
    params = MyParams(field1="test", field2=42)
    assert params.field1 == "test"
    assert params.field2 == 42


def test_params_allowed():
    allowed = MyParams.allowed()
    assert isinstance(allowed, set)
    assert "field1" in allowed
    assert "field2" in allowed
    assert "field3" in allowed
    assert "_none_as_sentinel" not in allowed  # Private fields excluded


@dataclass(slots=True, frozen=True, init=False)
class MyParamsNoneSentinel(Params):
    """Test params class with None as sentinel"""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    field1: str = Unset


def test_params_is_sentinel_none_as_sentinel():
    # When _none_as_sentinel is True, None should be treated as sentinel
    assert MyParamsNoneSentinel._is_sentinel(None) is True
    assert MyParamsNoneSentinel._is_sentinel(Undefined) is True
    assert MyParamsNoneSentinel._is_sentinel(Unset) is True
    assert MyParamsNoneSentinel._is_sentinel("value") is False


def test_params_is_sentinel_default():
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
    with pytest.raises(ValueError, match="Missing required parameter"):
        MyParamsStrict(field1="value")  # field2 is missing and strict=True


# ============================================================================
# Test DataClass - Lines 214, 246-248, 251-253, etc.
# ============================================================================


@dataclass(slots=True)
class MyDataClass(DataClass):
    """Test data class"""

    field1: str = Unset
    field2: int = Unset


def test_dataclass_valid():
    obj = MyDataClass(field1="test", field2=42)
    assert obj.field1 == "test"
    assert obj.field2 == 42


def test_dataclass_allowed():
    allowed = MyDataClass.allowed()
    assert isinstance(allowed, set)
    assert "field1" in allowed
    assert "field2" in allowed


@dataclass(slots=True)
class MyDataClassStrict(DataClass):
    """Test data class with strict mode"""

    _config: ClassVar[ModelConfig] = ModelConfig(strict=True)
    field1: str = Unset


def test_dataclass_strict_mode():
    with pytest.raises(ValueError, match="Missing required parameter"):
        MyDataClassStrict()  # Missing required field in strict mode


@dataclass(slots=True)
class MyDataClassPrefillUnset(DataClass):
    """Test data class with prefill_unset"""

    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=True)
    field1: str = field(default=Undefined)


def test_dataclass_prefill_unset():
    obj = MyDataClassPrefillUnset()
    # Field initialized to Undefined should be prefilled with Unset
    assert obj.field1 is Unset


@dataclass(slots=True)
class MyDataClassNoneSentinel(DataClass):
    """Test data class with None as sentinel"""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    field1: str = None


def test_dataclass_is_sentinel_none():
    assert MyDataClassNoneSentinel._is_sentinel(None) is True
    assert MyDataClassNoneSentinel._is_sentinel(Undefined) is True
    assert MyDataClassNoneSentinel._is_sentinel(Unset) is True


def test_dataclass_to_dict():
    obj = MyDataClass(field1="test", field2=42)
    result = obj.to_dict()
    assert "field1" in result
    assert "field2" in result


def test_dataclass_to_dict_exclude():
    obj = MyDataClass(field1="test", field2=42)
    result = obj.to_dict(exclude={"field2"})
    assert "field1" in result
    assert "field2" not in result


def test_dataclass_with_updates():
    obj = MyDataClass(field1="test", field2=42)
    updated = obj.with_updates(field2=100)
    assert updated.field1 == "test"
    assert updated.field2 == 100


def test_dataclass_hash():
    # DataClass needs to be frozen to be hashable, use Params instead
    params1 = MyParams(field1="test", field2=42)
    params2 = MyParams(field1="test", field2=42)
    hash1 = hash(params1)
    hash2 = hash(params2)
    assert isinstance(hash1, int)
    assert isinstance(hash2, int)


def test_dataclass_eq():
    obj1 = MyDataClass(field1="test", field2=42)
    obj2 = MyDataClass(field1="test", field2=42)
    obj3 = MyDataClass(field1="other", field2=99)
    assert obj1 == obj2
    assert obj1 != obj3


def test_dataclass_eq_not_dataclass():
    obj = MyDataClass(field1="test", field2=42)
    assert obj != "not a dataclass"
    assert obj != 42


# ============================================================================
# Test Params methods
# ============================================================================


def test_params_to_dict():
    params = MyParams(field1="test", field2=42)
    result = params.to_dict()
    assert "field1" in result
    assert "field2" in result


def test_params_to_dict_exclude():
    params = MyParams(field1="test", field2=42)
    result = params.to_dict(exclude={"field2"})
    assert "field1" in result
    assert "field2" not in result


def test_params_with_updates():
    params = MyParams(field1="test", field2=42)
    updated = params.with_updates(field2=100)
    assert updated.field1 == "test"
    assert updated.field2 == 100


def test_params_hash():
    params1 = MyParams(field1="test", field2=42)
    params2 = MyParams(field1="test", field2=42)
    # Just verify hash can be computed
    hash1 = hash(params1)
    hash2 = hash(params2)
    assert isinstance(hash1, int)
    assert isinstance(hash2, int)


def test_params_eq():
    params1 = MyParams(field1="test", field2=42)
    params2 = MyParams(field1="test", field2=42)
    params3 = MyParams(field1="other", field2=99)
    assert params1 == params2
    assert params1 != params3


def test_params_eq_not_params():
    params = MyParams(field1="test", field2=42)
    assert params != "not params"
    assert params != 42


def test_params_default_kw():
    params = MyParams(field1="test", field2=42)
    result = params.default_kw()
    assert isinstance(result, dict)
    assert result["field1"] == "test"
    assert result["field2"] == 42


# ============================================================================
# Test sentinel utilities
# ============================================================================


def test_is_sentinel():
    assert is_sentinel(Undefined) is True
    assert is_sentinel(Unset) is True
    assert is_sentinel(None) is False
    assert is_sentinel("value") is False
    assert is_sentinel(42) is False


def test_not_sentinel():
    assert not_sentinel(Undefined) is False
    assert not_sentinel(Unset) is False
    assert not_sentinel(None) is True
    assert not_sentinel("value") is True
    assert not_sentinel(42) is True
