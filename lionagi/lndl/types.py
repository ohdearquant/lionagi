# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Core types for LNDL."""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from lionagi.libs.validate.validate_boolean import validate_boolean
from lionagi.ln import json_dumps

Scalar = float | int | str | bool


@dataclass(slots=True, frozen=True)
class LvarMetadata:
    model: str
    field: str
    local_name: str
    value: str


@dataclass(slots=True, frozen=True)
class RLvarMetadata:
    local_name: str
    value: str


@dataclass(slots=True, frozen=True)
class LactMetadata:
    model: str | None
    field: str | None
    local_name: str
    call: str


@dataclass(slots=True, frozen=True)
class ParsedConstructor:
    class_name: str
    kwargs: dict[str, Any]
    raw: str

    @property
    def has_dict_unpack(self) -> bool:
        return any(k.startswith("**") for k in self.kwargs)


@dataclass(slots=True, frozen=True)
class ActionCall:
    name: str
    function: str
    arguments: dict[str, Any]
    raw_call: str


@dataclass(slots=True, frozen=True)
class LNDLOutput:
    fields: dict[str, BaseModel | ActionCall | Scalar]
    lvars: dict[str, str] | dict[str, LvarMetadata]
    lacts: dict[str, LactMetadata]
    actions: dict[str, ActionCall]
    raw_out_block: str

    def __getitem__(self, key: str) -> BaseModel | ActionCall | Scalar:
        return self.fields[key]

    def __getattr__(self, key: str) -> BaseModel | ActionCall | Scalar:
        if key in ("fields", "lvars", "lacts", "actions", "raw_out_block"):
            return object.__getattribute__(self, key)
        try:
            return self.fields[key]
        except KeyError:
            raise AttributeError(key) from None


def has_action_calls(model: BaseModel) -> bool:
    def _check_value(value: Any) -> bool:
        if isinstance(value, ActionCall):
            return True
        if isinstance(value, BaseModel):
            return has_action_calls(value)
        if isinstance(value, list | tuple | set):
            return any(_check_value(item) for item in value)
        if isinstance(value, dict):
            return any(_check_value(v) for v in value.values())
        return False

    return any(_check_value(value) for value in model.__dict__.values())


def ensure_no_action_calls(model: BaseModel) -> BaseModel:
    def _find_action_call_fields(obj: Any, path: str = "") -> list[str]:
        paths = []
        if isinstance(obj, ActionCall):
            return [path] if path else ["<root>"]
        if isinstance(obj, BaseModel):
            for field_name, value in obj.__dict__.items():
                field_path = f"{path}.{field_name}" if path else field_name
                paths.extend(_find_action_call_fields(value, field_path))
        elif isinstance(obj, list | tuple | set):
            for idx, item in enumerate(obj):
                paths.extend(_find_action_call_fields(item, f"{path}[{idx}]"))
        elif isinstance(obj, dict):
            for key, value in obj.items():
                paths.extend(_find_action_call_fields(value, f"{path}[{key!r}]"))
        return paths

    if has_action_calls(model):
        model_name = type(model).__name__
        action_call_fields = _find_action_call_fields(model)
        fields_str = ", ".join(action_call_fields[:3])
        if len(action_call_fields) > 3:
            fields_str += f" (and {len(action_call_fields) - 3} more)"
        raise ValueError(
            f"{model_name} contains unexecuted actions in fields: {fields_str}. "
            f"Call revalidate_with_action_results() before using this model."
        )
    return model


_SCALAR_TYPES: tuple[type, ...] = (str, int, float, bool)


def _unwrap_scalar(annotation: Any) -> type | None:
    """Extract the scalar type from a plain or Optional scalar annotation.

    Returns the scalar type if *annotation* is ``str``, ``int``, ``float``,
    ``bool``, or a ``Union``/PEP-604 union of one of those with ``None``.
    Returns ``None`` otherwise.

    Examples::

        _unwrap_scalar(str)          → str
        _unwrap_scalar(str | None)   → str
        _unwrap_scalar(int | None)   → int
        _unwrap_scalar(list[str])    → None
    """
    if annotation in _SCALAR_TYPES:
        return annotation  # type: ignore[return-value]

    # Handle both typing.Union[X, None] and X | None (PEP 604 UnionType)
    origin = get_origin(annotation)
    if origin is Union or isinstance(annotation, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1 and args[0] in _SCALAR_TYPES:
            return args[0]  # type: ignore[return-value]

    return None


def _coerce_result(result: Any, target_type: Any) -> Any:
    """Coerce a tool result to match the target field type.

    Handles both plain scalar types (``str``, ``int``, ``float``, ``bool``)
    and optional scalar types (e.g. ``str | None``, ``Optional[int]``).
    Dict results targeting a scalar are serialised to JSON via
    :func:`lionagi.ln.json_dumps`.
    """
    scalar = _unwrap_scalar(target_type)
    if scalar is None:
        return result
    # A legitimately-None result for an Optional scalar (str | None, int | None)
    # must pass through untouched. Coercing would corrupt it: scalar(None) yields
    # the literal "None" for str and raises for int/float. Leaving it None lets
    # model_validate accept it for Optional fields and raise a clear error for
    # required ones — never silently fabricate a value.
    if result is None:
        return None
    if isinstance(result, dict):
        return json_dumps(result)
    if not isinstance(result, scalar):
        # bool(str) uses Python truthiness: bool('false') == True.
        # Use validate_boolean so that 'false'/'0'/'no' → False and
        # 'true'/'1'/'yes' → True, matching common tool return conventions.
        if scalar is bool:
            return validate_boolean(result)
        return scalar(result)
    return result


def _revalidate_model(
    model: BaseModel,
    action_results: dict[str, Any],
) -> BaseModel:
    """Replace ActionCall placeholders in a single model with actual results."""
    kwargs = {}
    changed = False
    for field_name, value in model.__dict__.items():
        if isinstance(value, ActionCall):
            if value.name not in action_results:
                raise ValueError(
                    f"Action '{value.name}' in field '{field_name}' has no execution result. "
                    f"Available results: {list(action_results.keys())}"
                )
            field_info = type(model).model_fields.get(field_name)
            target_type = field_info.annotation if field_info else None
            kwargs[field_name] = _coerce_result(action_results[value.name], target_type)
            changed = True
        elif isinstance(value, BaseModel) and has_action_calls(value):
            kwargs[field_name] = _revalidate_model(value, action_results)
            changed = True
        else:
            kwargs[field_name] = value

    if not changed:
        return model
    return type(model).model_validate(kwargs)


def revalidate_with_action_results(
    model: BaseModel,
    action_results: dict[str, Any],
) -> BaseModel:
    return _revalidate_model(model, action_results)
