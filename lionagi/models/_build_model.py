# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility wrapper for legacy FieldModel-based Pydantic construction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.fields import FieldInfo

from lionagi.adapters.spec_adapters._pydantic_builder import _build_pydantic_model

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
    """Build through the target-owned builder while retaining FieldModel inputs."""
    field_descriptions = field_descriptions or {}
    override_fields: dict[str, FieldInfo] = {}
    collected_validators: dict = dict(validators) if validators else {}

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
            override_fields[fm.name] = field
            if fm.field_validator:
                collected_validators.update(fm.field_validator)

    return _build_pydantic_model(
        name=name,
        parameter_fields=parameter_fields,
        override_fields=override_fields,
        base_type=base_type,
        exclude_fields=exclude_fields,
        inherit_base=inherit_base,
        config_dict=config_dict,
        doc=doc,
        frozen=frozen,
        validators=collected_validators,
    )
