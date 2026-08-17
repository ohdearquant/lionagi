# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic-only model construction for neutral Spec declarations."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, create_model
from pydantic.fields import FieldInfo

from lionagi.ln import copy

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ()


def _build_pydantic_model(
    *,
    name: str | None = None,
    parameter_fields: Mapping[str, FieldInfo] | None = None,
    override_fields: Mapping[str, FieldInfo] | None = None,
    base_type: type[BaseModel] | None = None,
    exclude_fields: list[str] | None = None,
    inherit_base: bool = True,
    config_dict: ConfigDict | dict[str, Any] | None = None,
    doc: str | None = None,
    frozen: bool = False,
    validators: Mapping[str, Any] | None = None,
) -> type[BaseModel]:
    """Build one Pydantic model from already-materialized target fields.

    Precedence is intentionally compatibility-preserving: declared parameter fields,
    then fields copied from ``base_type``, then explicit override fields.
    """
    if base_type is not None and not (
        inspect.isclass(base_type) and issubclass(base_type, BaseModel)
    ):
        raise ValueError(f"base_type must be BaseModel subclass, got {base_type}")

    excluded = frozenset(exclude_fields or ())
    fields: dict[str, FieldInfo] = {}

    for source_name, source in (
        ("parameter_fields", parameter_fields),
        ("override_fields", override_fields),
    ):
        if source:
            for field_name, field_info in source.items():
                if not isinstance(field_info, FieldInfo):
                    raise ValueError(
                        f"{source_name} must contain FieldInfo instances, "
                        f"got {type(field_info)} for field '{field_name}'"
                    )

    if parameter_fields:
        fields.update(copy(dict(parameter_fields)))

    if base_type is not None:
        base_fields = copy(base_type.model_fields)
        if excluded:
            base_fields = {key: value for key, value in base_fields.items() if key not in excluded}
        fields.update(base_fields)

    if override_fields:
        fields.update(copy(dict(override_fields)))

    model_name = name
    if model_name is None and base_type is not None:
        class_name = getattr(base_type, "class_name", None)
        model_name = class_name() if callable(class_name) else (class_name or base_type.__name__)
    if model_name is None:
        model_name = "GeneratedModel"

    use_base = None
    if inherit_base and base_type is not None:
        if not any(field_name in excluded for field_name in base_type.model_fields):
            use_base = base_type

    use_fields = {key: (value.annotation, value) for key, value in fields.items()}
    model = create_model(
        model_name,
        __base__=use_base,
        __config__=config_dict or None,
        __doc__=doc or None,
        __validators__=dict(validators) if validators else None,
        **use_fields,
    )
    if frozen:
        model.model_config["frozen"] = True
    return model
