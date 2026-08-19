# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic adapter for Spec system."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, cast

from lionagi.ln._cache import BoundedLRUCache
from lionagi.ln._structural import _IdentityKey, _structural_key, _try_stable_cache_key
from lionagi.ln.types import is_sentinel

from ._protocol import SpecAdapter

__all__ = ("PydanticSpecAdapter",)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

    from lionagi.ln.types import Operable, Spec


# Shared across identical constructions — callers must not mutate a returned model class.
# LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0 disables sharing entirely.
# Weakly, so the entry shares a model that something is using and never keeps one
# alive on its own. A declaration's callables are reachable from the model it built,
# so an entry holding the model strongly outlives every name they had.
_model_type_cache: BoundedLRUCache[Any, type[BaseModel]] = BoundedLRUCache(
    "LIONAGI_OPERATIVE_MODEL_CACHE_SIZE", 512, weak_values=True
)


def _model_type_cache_key(
    *,
    adapter_type: type,
    base_type: type[BaseModel] | None,
    model_name: str,
    declaration: object,
    doc: str | None,
) -> Any | None:
    """Build an identity-safe cache key, or opt out for mutable field metadata."""
    if base_type is None:
        return None

    declaration_key = _try_stable_cache_key(declaration)
    if declaration_key is None:
        return None
    return (
        "pydantic-model-v1",
        _IdentityKey(adapter_type),
        _IdentityKey(base_type),
        _structural_key(model_name),
        declaration_key,
        _structural_key(doc),
    )


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
        if is_sentinel(v):
            return None

        from pydantic import field_validator

        field_name = spec.name if isinstance(spec.name, str) else "field"
        return {f"{field_name}_validator": field_validator(field_name)(v)}

    @classmethod
    def create_model(
        cls,
        op: Operable,
        model_name: str,
        include: Collection[str] | None = None,
        exclude: Collection[str] | None = None,
        base_type: type[BaseModel] | None = None,
        doc: str | None = None,
    ) -> type[BaseModel]:
        """Generate Pydantic BaseModel from Operable."""
        from lionagi.models._build_model import build_model_type

        use_specs = op.get_specs(include=include, exclude=exclude)
        for index, spec in enumerate(use_specs):
            if not isinstance(spec.name, str):
                raise ValueError(
                    "Pydantic model fields require a string name; "
                    f"unnamed or non-string Spec found at index {index}"
                )
        cache_key = _model_type_cache_key(
            adapter_type=cls,
            base_type=base_type,
            model_name=model_name,
            declaration=op if use_specs is op.__op_fields__ else use_specs,
            doc=doc,
        )

        def build() -> type[BaseModel]:
            use_fields = {cast(str, i.name): cls.create_field(i) for i in use_specs}

            validators = {}
            for spec in use_specs:
                validator = cls.create_validator(spec)
                if validator:
                    validators.update(validator)

            result = build_model_type(
                name=model_name,
                parameter_fields=use_fields,
                base_type=base_type,
                inherit_base=True,
                doc=doc,
                validators=validators,
            )
            result.model_rebuild()
            return result

        model_cls = (
            build() if cache_key is None else _model_type_cache.get_or_create(cache_key, build)
        )
        if not model_cls.__pydantic_complete__:
            model_cls.model_rebuild()
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
