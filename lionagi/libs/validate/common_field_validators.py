# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel, JsonValue

from lionagi.ln import copy
from lionagi.utils import UNDEFINED, to_list

from .validate_boolean import validate_boolean


def validate_boolean_field(cls, value, default=None) -> bool | None:
    """Coerce value to bool via validate_boolean; return default on failure."""
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
    """Flatten value to list and verify all items are dtype; raises ValueError on mixed types."""
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
    """Return value if non-empty string; None on blank/None; raises ValueError if strict and not a string."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None

    if not isinstance(value, str):
        if strict:
            raise ValueError(f"{field_name or 'Field'} must be a string.")
        return None

    return value


def validate_nullable_jsonvalue_field(cls, value) -> JsonValue | None:
    """Return None on empty/None input; otherwise return value unchanged."""
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
    """Return value if callable (or None/UNDEFINED when undefind_able=True); raises ValueError otherwise."""
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
