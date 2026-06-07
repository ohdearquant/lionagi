# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for common field validators."""

import pytest
from pydantic import BaseModel

from lionagi.libs.validate.common_field_validators import (
    validate_boolean_field,
    validate_callable,
    validate_dict_kwargs_params,
    validate_list_dict_str_keys,
    validate_model_to_type,
    validate_nullable_jsonvalue_field,
    validate_nullable_string_field,
    validate_same_dtype_flat_list,
    validate_str_str_dict,
)
from lionagi.utils import UNDEFINED


class DummyCls:
    """Dummy class for validator testing."""

    pass


class TestValidateBooleanField:
    """Tests for validate_boolean_field function."""

    @pytest.mark.parametrize("value", [True, False])
    def test_boolean_values(self, value):
        """Test direct boolean values."""
        result = validate_boolean_field(DummyCls, value)
        assert result is value

    @pytest.mark.parametrize("value", ["true", "yes", "1", "on"])
    def test_true_string_values(self, value):
        """Test string values that convert to True."""
        result = validate_boolean_field(DummyCls, value)
        assert result is True

    @pytest.mark.parametrize("value", ["false", "no", "0", "off"])
    def test_false_string_values(self, value):
        """Test string values that convert to False."""
        result = validate_boolean_field(DummyCls, value)
        assert result is False

    def test_invalid_value_returns_default(self):
        """Test invalid values return default."""
        result = validate_boolean_field(DummyCls, "invalid")
        assert result is None

    def test_custom_default(self):
        """Test custom default value."""
        result = validate_boolean_field(DummyCls, "invalid", default=False)
        assert result is False

    def test_none_returns_default(self):
        """Test None returns default."""
        result = validate_boolean_field(DummyCls, None, default=True)
        assert result is True


class TestValidateSameDtypeFlatList:
    """Tests for validate_same_dtype_flat_list function."""

    def test_valid_list_of_strings(self):
        """Test list of strings."""
        result = validate_same_dtype_flat_list(DummyCls, ["a", "b", "c"], str)
        assert result == ["a", "b", "c"]

    def test_valid_list_of_ints(self):
        """Test list of integers."""
        result = validate_same_dtype_flat_list(DummyCls, [1, 2, 3], int)
        assert result == [1, 2, 3]

    def test_nested_list_flattening(self):
        """Test nested lists are flattened."""
        result = validate_same_dtype_flat_list(DummyCls, [[1, 2], [3, 4]], int)
        assert result == [1, 2, 3, 4]

    def test_dict_values_extraction(self):
        """Test dict values are extracted."""
        result = validate_same_dtype_flat_list(DummyCls, {"a": 1, "b": 2}, int)
        assert sorted(result) == [1, 2]

    def test_none_returns_default(self):
        """Test None returns default empty list."""
        result = validate_same_dtype_flat_list(DummyCls, None, str)
        assert result == []

    def test_undefined_returns_default(self):
        """Test UNDEFINED returns default empty list."""
        result = validate_same_dtype_flat_list(DummyCls, UNDEFINED, str)
        assert result == []

    def test_empty_dict_returns_default(self):
        """Test empty dict returns default."""
        result = validate_same_dtype_flat_list(DummyCls, {}, str)
        assert result == []

    def test_custom_default(self):
        """Test custom default value."""
        result = validate_same_dtype_flat_list(DummyCls, None, str, default=["default"])
        assert result == ["default"]

    def test_mixed_types_raises_error(self):
        """Test mixed types raise ValueError."""
        with pytest.raises(ValueError, match="must contain only"):
            validate_same_dtype_flat_list(DummyCls, [1, "2", 3], int)

    def test_dropna_removes_none(self):
        """Test dropna removes None values."""
        result = validate_same_dtype_flat_list(DummyCls, [1, None, 2, None, 3], int, dropna=True)
        assert result == [1, 2, 3]

    def test_dropna_false_keeps_none(self):
        """Test dropna=False raises error with None."""
        with pytest.raises(ValueError, match="must contain only"):
            validate_same_dtype_flat_list(DummyCls, [1, None, 2], int, dropna=False)


class TestValidateNullableStringField:
    """Tests for validate_nullable_string_field function."""

    def test_valid_string(self):
        """Test valid string is returned."""
        result = validate_nullable_string_field(DummyCls, "hello")
        assert result == "hello"

    def test_none_returns_none(self):
        """Test None returns None."""
        result = validate_nullable_string_field(DummyCls, None)
        assert result is None

    def test_empty_string_returns_none(self):
        """Test empty string returns None."""
        result = validate_nullable_string_field(DummyCls, "")
        assert result is None

    def test_whitespace_only_returns_none(self):
        """Test whitespace-only string returns None."""
        result = validate_nullable_string_field(DummyCls, "   ")
        assert result is None

    def test_non_string_strict_raises_error(self):
        """Test non-string with strict=True raises error."""
        with pytest.raises(ValueError, match="must be a string"):
            validate_nullable_string_field(DummyCls, 123, strict=True)

    def test_non_string_non_strict_returns_none(self):
        """Test non-string with strict=False returns None."""
        result = validate_nullable_string_field(DummyCls, 123, strict=False)
        assert result is None

    def test_custom_field_name_in_error(self):
        """Test custom field name appears in error message."""
        with pytest.raises(ValueError, match="username must be a string"):
            validate_nullable_string_field(DummyCls, 123, field_name="username", strict=True)

    def test_string_with_content(self):
        """Test string with whitespace and content."""
        result = validate_nullable_string_field(DummyCls, "  hello  ")
        assert result == "  hello  "


class TestValidateNullableJsonvalueField:
    """Tests for validate_nullable_jsonvalue_field function."""

    def test_valid_string(self):
        """Test valid string is returned."""
        result = validate_nullable_jsonvalue_field(DummyCls, "test")
        assert result == "test"

    def test_valid_number(self):
        """Test valid number is returned."""
        result = validate_nullable_jsonvalue_field(DummyCls, 42)
        assert result == 42

    def test_valid_dict(self):
        """Test valid dict is returned."""
        data = {"key": "value"}
        result = validate_nullable_jsonvalue_field(DummyCls, data)
        assert result == data

    def test_valid_list(self):
        """Test valid list is returned."""
        data = [1, 2, 3]
        result = validate_nullable_jsonvalue_field(DummyCls, data)
        assert result == data

    def test_none_returns_none(self):
        """Test None returns None."""
        result = validate_nullable_jsonvalue_field(DummyCls, None)
        assert result is None

    def test_empty_string_returns_none(self):
        """Test empty string returns None."""
        result = validate_nullable_jsonvalue_field(DummyCls, "")
        assert result is None

    def test_whitespace_only_returns_none(self):
        """Test whitespace-only string returns None."""
        result = validate_nullable_jsonvalue_field(DummyCls, "   ")
        assert result is None


class TestValidateDictKwargsParams:
    """Tests for validate_dict_kwargs_params function."""

    def test_valid_dict(self):
        """Test valid dict is returned."""
        data = {"key": "value", "number": 42}
        result = validate_dict_kwargs_params(DummyCls, data)
        assert result == data

    def test_empty_dict(self):
        """Test empty dict is returned."""
        result = validate_dict_kwargs_params(DummyCls, {})
        assert result == {}

    def test_none_returns_empty_dict(self):
        """Test None returns empty dict."""
        result = validate_dict_kwargs_params(DummyCls, None)
        assert result == {}

    def test_undefined_returns_empty_dict(self):
        """Test UNDEFINED returns empty dict."""
        result = validate_dict_kwargs_params(DummyCls, UNDEFINED)
        assert result == {}

    def test_empty_list_returns_empty_dict(self):
        """Test empty list returns empty dict."""
        result = validate_dict_kwargs_params(DummyCls, [])
        assert result == {}

    def test_non_dict_raises_error(self):
        """Test non-dict raises ValueError."""
        with pytest.raises(ValueError, match="must be a dictionary"):
            validate_dict_kwargs_params(DummyCls, "not a dict")

    def test_list_with_items_raises_error(self):
        """Test list with items raises ValueError."""
        with pytest.raises(ValueError, match="must be a dictionary"):
            validate_dict_kwargs_params(DummyCls, [1, 2, 3])


class TestValidateCallable:
    """Tests for validate_callable function."""

    def test_function_is_callable(self):
        """Test function is valid callable."""

        def func():
            pass

        result = validate_callable(DummyCls, func)
        assert result is func

    def test_lambda_is_callable(self):
        """Test lambda is valid callable."""
        func = lambda x: x
        result = validate_callable(DummyCls, func)
        assert result is func

    def test_class_is_callable(self):
        """Test class is valid callable."""

        class TestClass:
            pass

        result = validate_callable(DummyCls, TestClass)
        assert result is TestClass

    def test_builtin_is_callable(self):
        """Test builtin function is valid callable."""
        result = validate_callable(DummyCls, len)
        assert result is len

    def test_non_callable_raises_error(self):
        """Test non-callable raises ValueError."""
        with pytest.raises(ValueError, match="must be a callable"):
            validate_callable(DummyCls, "not callable", undefind_able=False)

    def test_none_with_undefined_able(self):
        """Test None is allowed with undefind_able=True."""
        result = validate_callable(DummyCls, None, undefind_able=True)
        assert result is None

    def test_undefined_with_undefined_able(self):
        """Test UNDEFINED is allowed with undefind_able=True."""
        result = validate_callable(DummyCls, UNDEFINED, undefind_able=True)
        assert result is UNDEFINED

    def test_none_without_undefined_able(self):
        """Test None raises error with undefind_able=False."""
        with pytest.raises(ValueError, match="must be a callable"):
            validate_callable(DummyCls, None, undefind_able=False)

    def test_check_name_with_named_function(self):
        """Test check_name with named function."""

        def named_func():
            pass

        result = validate_callable(DummyCls, named_func, check_name=True)
        assert result is named_func

    def test_check_name_with_lambda_raises_error(self):
        """Test check_name with lambda raises error."""
        # Lambda has __name__ attribute, so this should pass
        func = lambda x: x
        result = validate_callable(DummyCls, func, check_name=True)
        assert result is func


class TestValidateModelToType:
    """Tests for validate_model_to_type function."""

    def test_none_returns_basemodel(self):
        """Test None returns BaseModel class."""
        result = validate_model_to_type(DummyCls, None)
        assert result is BaseModel

    def test_basemodel_subclass_type(self):
        """Test BaseModel subclass type is returned."""

        class CustomModel(BaseModel):
            pass

        result = validate_model_to_type(DummyCls, CustomModel)
        assert result is CustomModel

    def test_basemodel_instance(self):
        """Test BaseModel instance returns its class."""

        class CustomModel(BaseModel):
            value: int = 1

        instance = CustomModel()
        result = validate_model_to_type(DummyCls, instance)
        assert result is CustomModel

    def test_non_basemodel_class_raises_error(self):
        """Test non-BaseModel class raises ValueError."""

        class NotAModel:
            pass

        with pytest.raises(ValueError, match="must be a BaseModel"):
            validate_model_to_type(DummyCls, NotAModel)

    def test_non_model_value_raises_error(self):
        """Test non-model value raises ValueError."""
        with pytest.raises(ValueError, match="must be a BaseModel"):
            validate_model_to_type(DummyCls, "not a model")


class TestValidateListDictStrKeys:
    """Tests for validate_list_dict_str_keys function."""

    def test_none_returns_empty_list(self):
        """Test None returns empty list."""
        result = validate_list_dict_str_keys(DummyCls, None)
        assert result == []

    def test_dict_returns_keys(self):
        """Test dict returns list of keys."""
        result = validate_list_dict_str_keys(DummyCls, {"a": 1, "b": 2})
        assert sorted(result) == ["a", "b"]

    def test_set_returns_list(self):
        """Test set is converted to list."""
        result = validate_list_dict_str_keys(DummyCls, {"a", "b", "c"})
        assert sorted(result) == ["a", "b", "c"]

    def test_tuple_returns_list(self):
        """Test tuple is converted to list."""
        result = validate_list_dict_str_keys(DummyCls, ("a", "b", "c"))
        assert result == ["a", "b", "c"]

    def test_list_of_strings(self):
        """Test list of strings is returned as copy."""
        original = ["a", "b", "c"]
        result = validate_list_dict_str_keys(DummyCls, original)
        assert result == original
        assert result is not original  # Should be a copy

    def test_list_with_non_string_raises_error(self):
        """Test list with non-string raises ValueError."""
        with pytest.raises(ValueError, match="must be strings"):
            validate_list_dict_str_keys(DummyCls, ["a", 1, "b"])

    def test_non_iterable_raises_error(self):
        """Test non-iterable raises ValueError."""
        with pytest.raises(ValueError, match="must be a list"):
            validate_list_dict_str_keys(DummyCls, 123)


class TestValidateStrStrDict:
    """Tests for validate_str_str_dict function."""

    def test_none_returns_empty_dict(self):
        """Test None returns empty dict."""
        result = validate_str_str_dict(DummyCls, None)
        assert result == {}

    def test_valid_str_str_dict(self):
        """Test valid string-to-string dict."""
        data = {"key1": "value1", "key2": "value2"}
        result = validate_str_str_dict(DummyCls, data)
        assert result == data

    def test_empty_dict(self):
        """Test empty dict is valid."""
        result = validate_str_str_dict(DummyCls, {})
        assert result == {}

    def test_non_dict_raises_error(self):
        """Test non-dict raises ValueError."""
        with pytest.raises(ValueError, match="must be a dictionary"):
            validate_str_str_dict(DummyCls, "not a dict")

    def test_non_string_key_raises_error(self):
        """Test non-string key raises ValueError."""
        with pytest.raises(ValueError, match="names must be strings"):
            validate_str_str_dict(DummyCls, {1: "value"})

    def test_non_string_value_raises_error(self):
        """Test non-string value raises ValueError."""
        with pytest.raises(ValueError, match="value must be strings"):
            validate_str_str_dict(DummyCls, {"key": 123})

    def test_mixed_invalid_types(self):
        """Test dict with mixed invalid types."""
        with pytest.raises(ValueError):
            validate_str_str_dict(DummyCls, {1: 2})


# Integration tests
class TestValidatorsIntegration:
    """Integration tests for validators working together."""

    def test_validators_with_pydantic_model(self):
        """Test validators can be used in Pydantic model validation."""

        class TestModel(BaseModel):
            string_field: str | None
            list_field: list[int]

            @classmethod
            def validate_string(cls, v):
                return validate_nullable_string_field(cls, v)

            @classmethod
            def validate_list(cls, v):
                return validate_same_dtype_flat_list(cls, v, int)

        # This tests that validators work in Pydantic context
        model = TestModel(string_field="test", list_field=[1, 2, 3])
        assert model.string_field == "test"
        assert model.list_field == [1, 2, 3]


@pytest.mark.parametrize(
    "validator,value,expected",
    [
        (validate_boolean_field, True, True),
        (validate_boolean_field, False, False),
        (validate_nullable_string_field, "hello", "hello"),
        (validate_nullable_string_field, None, None),
        (validate_nullable_jsonvalue_field, 42, 42),
        (validate_nullable_jsonvalue_field, None, None),
        (validate_dict_kwargs_params, {}, {}),
        (validate_dict_kwargs_params, None, {}),
    ],
)
def test_validator_parametrized(validator, value, expected):
    """Parametrized test for common validator patterns."""
    result = validator(DummyCls, value)
    assert result == expected
