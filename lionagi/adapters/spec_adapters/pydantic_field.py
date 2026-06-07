# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic adapter for Spec system."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from lionagi.ln.types import is_sentinel

from ._protocol import SpecAdapter

__all__ = ("PydanticSpecAdapter",)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

    from lionagi.ln.types import Operable, Spec


@functools.lru_cache(maxsize=1)
def _get_pydantic_field_params() -> set[str]:
    """Get valid Pydantic Field parameters (cached, thread-safe)."""
    import inspect

    from pydantic import Field as PydanticField

    params = set(inspect.signature(PydanticField).parameters.keys())
    params.discard("kwargs")
    return params


class PydanticSpecAdapter(SpecAdapter):
    """Pydantic implementation of SpecAdapter."""

    @classmethod
    def create_field(cls, spec: Spec) -> FieldInfo:
        """Create a Pydantic FieldInfo object from Spec."""
        from pydantic import Field as PydanticField

        # Get valid Pydantic Field parameters (cached)
        pydantic_field_params = _get_pydantic_field_params()

        # Extract metadata for FieldInfo
        field_kwargs = {}

        if not is_sentinel(spec.metadata, none_as_sentinel=True):
            for meta in spec.metadata:
                if meta.key == "default":
                    # Handle callable defaults as default_factory
                    if callable(meta.value):
                        field_kwargs["default_factory"] = meta.value
                    else:
                        field_kwargs["default"] = meta.value
                elif meta.key == "validator":
                    # Validators are handled separately in create_model
                    continue
                elif meta.key in pydantic_field_params:
                    # Pass through standard Pydantic field attributes
                    field_kwargs[meta.key] = meta.value
                elif meta.key in {"nullable", "listable"}:
                    # These are FieldTemplate markers, don't pass to FieldInfo
                    pass
                else:
                    # Filter out unserializable objects from json_schema_extra
                    if isinstance(meta.value, type):
                        # Skip type objects - can't be serialized
                        continue

                    # Any other metadata goes in json_schema_extra
                    if "json_schema_extra" not in field_kwargs:
                        field_kwargs["json_schema_extra"] = {}
                    field_kwargs["json_schema_extra"][meta.key] = meta.value

        # Handle nullable case - ensure default is set if not already
        if (
            spec.is_nullable
            and "default" not in field_kwargs
            and "default_factory" not in field_kwargs
        ):
            field_kwargs["default"] = None

        field_info = PydanticField(**field_kwargs)
        field_info.annotation = spec.annotation

        return field_info

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
