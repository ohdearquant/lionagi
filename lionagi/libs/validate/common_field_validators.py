# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel, JsonValue

from lionagi.utils import UNDEFINED, copy, to_list

from .validate_boolean import validate_boolean


def validate_boolean_field(cls, value, default=None) -> bool | None:
    """Validate boolean field value.

    Args:
        cls: The validator class method.
        value: The value to validate as boolean.

    Returns:
        bool | None: The validated boolean value or None if invalid.
    """
    try:
        return validate_boolean(value)
    except Exception:
        return default


def validate_same_dtype_flat_list(
    cls,
    value,
    dtype: type,
    default=None,
    dropna: bool = True,
) -> list:
    """Validate list of same data type.

    Args:
        cls: The validator class method.
        value: The value to validate as a list.
        dtype: The data type to validate.

    Returns:
        list: The validated list of same data type.

    Raises:
        ValueError: If value is not a list or contains different data types.
    """
    if value in [None, UNDEFINED, {}]:
        return default if default is not None else []

    to_list_kwargs = {}
    to_list_kwargs["flatten"] = True
    to_list_kwargs["use_values"] = True
    if dropna:
        to_list_kwargs["dropna"] = True
    value = to_list(value, **to_list_kwargs)

    if not all(isinstance(i, dtype) for i in value):
        raise ValueError(f"List must contain only {dtype.__name__} values.")

    return value


def validate_nullable_string_field(cls, value, field_name: str = None, strict=True) -> str | None:
    """Validate nullable string field.

    Args:
        cls: The validator class method.
        value: The value to validate as a string.

    Returns:
        str | None: The validated string value or None if invalid.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None

    if not isinstance(value, str):
        if strict:
            raise ValueError(f"{field_name or 'Field'} must be a string.")
        return None

    return value


def validate_nullable_jsonvalue_field(cls, value) -> JsonValue | None:
    """Validates that the instruction is not empty or None and is in the correct format.

    Args:
        cls: The validator class method.
        value (JsonValue | None): The instruction value to validate.

    Returns:
        JsonValue | None: The validated instruction or None if invalid.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def validate_dict_kwargs_params(cls, value) -> dict:
    """Validate validator kwargs."""
    if value in [None, UNDEFINED, []]:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Validator kwargs must be a dictionary")
    return value


def validate_callable(cls, value, undefind_able: bool = True, check_name: bool = False) -> callable:
    """Validate strict callable function.

    Args:
        cls: The validator class method.
        value: The value to validate as a callable function.

    Returns:
        callable: The validated callable function.

    Raises:
        ValueError: If value is not callable.
    """
    if not callable(value):
        if undefind_able and value in [None, UNDEFINED]:
            pass
        else:
            raise ValueError("Value must be a callable function")
    if check_name and not hasattr(value, "__name__"):
        raise ValueError("Function must have a name.")
    return value


def validate_model_to_type(cls, value):
    if value is None:
        return BaseModel
    if isinstance(value, type) and issubclass(value, BaseModel):
        return value
    if isinstance(value, BaseModel):
        return value.__class__
    raise ValueError("Base must be a BaseModel subclass or instance.")


def validate_list_dict_str_keys(cls, value):
    if value is None:
        return []
    if isinstance(value, dict):
        value = list(value.keys())
    if isinstance(value, set | tuple):
        value = list(value)
    if isinstance(value, list):
        if not all(isinstance(i, str) for i in value):
            raise ValueError("Field names must be strings.")
        return copy(value)
    raise ValueError("Fields must be a list, set, or dictionary.")


def validate_str_str_dict(cls, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Field must be a dictionary.")
    for k, v in value.items():
        if not isinstance(k, str):
            raise ValueError("Field names must be strings.")
        if not isinstance(v, str):
            raise ValueError("Field value must be strings.")
    return value
