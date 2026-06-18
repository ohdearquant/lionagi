# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Build a Pydantic model from field sources. No cache"""

from __future__ import annotations

import inspect

from pydantic import BaseModel, ConfigDict, create_model
from pydantic.fields import FieldInfo

from lionagi.ln import copy

from .field_model import FieldModel

__all__ = ("build_model_type",)


def build_model_type(
    *,
    name: str | None = None,
    parameter_fields: dict[str, FieldInfo] | None = None,
    field_models: list[FieldModel] | None = None,
    base_type: type[BaseModel] | None = None,
    exclude_fields: list[str] | None = None,
    field_descriptions: dict[str, str] | None = None,
    inherit_base: bool = True,
    config_dict: ConfigDict | dict | None = None,
    doc: str | None = None,
    frozen: bool = False,
    validators: dict | None = None,
) -> type[BaseModel]:
    # Fresh model per call, no cache: a shared cache keyed on a structural hash
    # cross-wires distinct same-named/shaped classes onto one another's models.
    # Field precedence (later wins): parameter_fields → base_type → field_models.
    if base_type is not None and not (
        inspect.isclass(base_type) and issubclass(base_type, BaseModel)
    ):
        raise ValueError(f"base_type must be BaseModel subclass, got {base_type}")

    exclude_fields = exclude_fields or []
    field_descriptions = field_descriptions or {}
    fields: dict[str, FieldInfo] = {}
    collected_validators: dict = dict(validators) if validators else {}

    if parameter_fields:
        for fname, field_info in parameter_fields.items():
            if not isinstance(field_info, FieldInfo):
                raise ValueError(
                    f"parameter_fields must contain FieldInfo instances, "
                    f"got {type(field_info)} for field '{fname}'"
                )
        fields.update(copy(parameter_fields))

    if base_type is not None:
        base_fields = copy(base_type.model_fields)
        if exclude_fields:
            base_fields = {k: v for k, v in base_fields.items() if k not in exclude_fields}
        fields.update(base_fields)

    if field_models:
        fms = [field_models] if isinstance(field_models, FieldModel) else field_models
        for fm in fms:
            if not isinstance(fm, FieldModel):
                raise ValueError(f"field_models must contain FieldModel instances, got {type(fm)}")
        fms = [
            fm.with_description(field_descriptions[fm.name])
            if fm.name in field_descriptions
            else fm
            for fm in fms
        ]
        for fm in fms:
            field = fm.create_field()
            field.annotation = fm.annotation
            fields[fm.name] = field
            if fm.field_validator:
                collected_validators.update(fm.field_validator)

    model_name = name
    if model_name is None and base_type is not None:
        class_name = getattr(base_type, "class_name", None)
        model_name = class_name() if callable(class_name) else (class_name or base_type.__name__)
    if model_name is None:
        model_name = "GeneratedModel"

    use_base = None
    if inherit_base and base_type is not None:
        if not any(f in exclude_fields for f in base_type.model_fields):
            use_base = base_type

    use_fields = {k: (v.annotation, v) for k, v in fields.items()}
    model = create_model(
        model_name,
        __base__=use_base,
        __config__=config_dict or None,
        __doc__=doc or None,
        __validators__=collected_validators or None,
        **use_fields,
    )
    if frozen:
        model.model_config["frozen"] = True
    return model
