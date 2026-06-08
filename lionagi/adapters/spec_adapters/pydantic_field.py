# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic adapter for Spec system."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lionagi.ln.types import is_sentinel

from ._protocol import SpecAdapter

__all__ = ("PydanticSpecAdapter",)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

    from lionagi.ln.types import Operable, Spec


class PydanticSpecAdapter(SpecAdapter):
    """Pydantic implementation of SpecAdapter."""

    @classmethod
    def create_field(cls, spec: Spec) -> FieldInfo:
        """Create a Pydantic FieldInfo object from Spec."""
        from lionagi.models.field_model import FieldModel

        fm = FieldModel(spec.base_type, metadata=spec.metadata)
        return fm.create_field()

    @classmethod
    def create_validator(cls, spec: Spec) -> dict | None:
        """Create Pydantic field_validator from Spec metadata."""
        v = spec.get("validator")
        if is_sentinel(v) or v is None:
            return None

        from pydantic import field_validator

        field_name = spec.name or "field"
        return {f"{field_name}_validator": field_validator(field_name)(v)}

    @classmethod
    def create_model(
        cls,
        op: Operable,
        model_name: str,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        base_type: type[BaseModel] | None = None,
        doc: str | None = None,
    ) -> type[BaseModel]:
        """Generate Pydantic BaseModel from Operable."""
        from lionagi.models.model_params import ModelParams

        use_specs = op.get_specs(include=include, exclude=exclude)
        use_fields = {i.name: cls.create_field(i) for i in use_specs if i.name}

        # Collect validators from specs
        validators = {}
        for spec in use_specs:
            if spec.name:
                v = cls.create_validator(spec)
                if v:
                    validators.update(v)

        params = ModelParams(
            name=model_name,
            parameter_fields=use_fields,
            base_type=base_type,
            inherit_base=True,
            doc=doc,
        )
        # Inject spec validators into ModelParams before model creation
        if validators:
            existing = dict(params._validators) if params._validators else {}
            existing.update(validators)
            object.__setattr__(params, "_validators", existing)

        model_cls = params.create_new_model()

        model_cls.model_rebuild()
        return model_cls

    @classmethod
    def fuzzy_match_fields(
        cls, data: dict, model_cls: type[BaseModel], strict: bool = False
    ) -> dict:
        """Match data keys to Pydantic model fields with fuzzy matching.

        Args:
            data: Raw data dictionary
            model_cls: Pydantic model class
            strict: If True, raise on unmatched; if False, force coercion

        Returns:
            Dictionary with keys matched to model fields
        """
        from lionagi.ln import fuzzy_match_keys
        from lionagi.ln.types import Undefined

        handle_mode = "raise" if strict else "force"

        matched = fuzzy_match_keys(data, model_cls.model_fields, handle_unmatched=handle_mode)

        # Filter out undefined values
        return {k: v for k, v in matched.items() if v != Undefined}

    @classmethod
    def validate_model(cls, model_cls: type[BaseModel], data: dict) -> BaseModel:
        """Validate dict data into Pydantic model instance."""
        return model_cls.model_validate(data)

    @classmethod
    def dump_model(cls, instance: BaseModel) -> dict:
        """Dump Pydantic model instance to dictionary."""
        return instance.model_dump()
