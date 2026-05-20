# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Core types for LNDL."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

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
        return self.fields[key]


def has_action_calls(model: BaseModel) -> bool:
    def _check_value(value: Any) -> bool:
        if isinstance(value, ActionCall):
            return True
        if isinstance(value, BaseModel):
            return has_action_calls(value)
        if isinstance(value, (list, tuple, set)):
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
        elif isinstance(obj, (list, tuple, set)):
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


def _coerce_result(result: Any, target_type: type | None) -> Any:
    """Coerce a tool result to match the target field type."""
    if target_type in (str, int, float, bool) and isinstance(result, dict):
        import json

        return json.dumps(result, ensure_ascii=False)
    if target_type in (str, int, float, bool) and not isinstance(result, target_type):
        return target_type(result)
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
            field_info = model.model_fields.get(field_name)
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
