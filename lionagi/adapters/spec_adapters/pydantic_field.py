# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic adapter for Spec system."""

from __future__ import annotations

from types import FunctionType, GenericAlias, UnionType
from typing import TYPE_CHECKING

from lionagi.ln._cache import BoundedLRUCache
from lionagi.ln.types import is_sentinel

from ._protocol import SpecAdapter

__all__ = ("PydanticSpecAdapter",)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

    from lionagi.ln.types import Operable, Spec


# Model classes, unlike Operative instances, contain no request/response state
# and are shared across identical constructions. The sharing contract: callers
# must not mutate a returned model class (mutation would be visible to every
# later identical construction); LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0 disables
# sharing entirely. The LRU bounds strong references to dynamically-created
# base classes and their generated models.
_model_type_cache: BoundedLRUCache[tuple[type[BaseModel], tuple], type[BaseModel]] = (
    BoundedLRUCache("LIONAGI_OPERATIVE_MODEL_CACHE_SIZE", 512)
)


def _is_cache_safe_value(value: object) -> bool:
    """Return whether a Spec metadata value is immutable enough for a shared model type."""
    if value is None or isinstance(value, (bool, bytes, float, int, str, type)):
        return True
    if isinstance(value, (FunctionType, GenericAlias, UnionType)):
        return True
    if isinstance(value, tuple):
        return all(_is_cache_safe_value(item) for item in value)
    if isinstance(value, frozenset):
        return all(_is_cache_safe_value(item) for item in value)
    return False


def _model_type_cache_key(
    *,
    base_type: type[BaseModel] | None,
    model_name: str,
    specs: tuple[Spec, ...],
    include: set[str] | None,
    exclude: set[str] | None,
    doc: str | None,
) -> tuple[type[BaseModel], tuple] | None:
    """Build an identity-safe cache key, or opt out for mutable field metadata."""
    if base_type is None:
        return None

    if not all(
        _is_cache_safe_value(spec.base_type)
        and all(_is_cache_safe_value(meta.value) for meta in spec.metadata)
        for spec in specs
    ):
        return None

    spec_options = tuple(
        (spec.base_type, tuple((meta.key, meta.value) for meta in spec.metadata)) for spec in specs
    )
    options = (
        model_name,
        spec_options,
        frozenset(include) if include is not None else None,
        frozenset(exclude) if exclude is not None else None,
        doc,
    )
    try:
        hash((base_type, options))
    except TypeError:
        return None
    return base_type, options


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
        from lionagi.models._build_model import build_model_type

        use_specs = op.get_specs(include=include, exclude=exclude)
        cache_key = _model_type_cache_key(
            base_type=base_type,
            model_name=model_name,
            specs=use_specs,
            include=include,
            exclude=exclude,
            doc=doc,
        )
        if cache_key is not None:
            cached = _model_type_cache.get(cache_key)
            if cached is not None:
                if not cached.__pydantic_complete__:
                    cached.model_rebuild()
                return cached

        use_fields = {i.name: cls.create_field(i) for i in use_specs if i.name}

        # Collect validators from specs
        validators = {}
        for spec in use_specs:
            if spec.name:
                v = cls.create_validator(spec)
                if v:
                    validators.update(v)

        model_cls = build_model_type(
            name=model_name,
            parameter_fields=use_fields,
            base_type=base_type,
            inherit_base=True,
            doc=doc,
            validators=validators,
        )

        model_cls.model_rebuild()
        if cache_key is not None:
            _model_type_cache.put(cache_key, model_cls)
        return model_cls

    @classmethod
    def fuzzy_match_fields(
        cls, data: dict, model_cls: type[BaseModel], strict: bool = False
    ) -> dict:
        """Match data keys to Pydantic model fields with fuzzy matching; strict=True raises on miss."""
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
