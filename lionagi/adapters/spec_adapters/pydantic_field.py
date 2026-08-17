# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic adapter for Spec system."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, cast, get_origin

from lionagi.ln._cache import BoundedLRUCache
from lionagi.ln._lazy_init import LazyInit
from lionagi.ln._structural import _IdentityKey, _structural_key, _try_stable_cache_key
from lionagi.ln.types import CommonMeta, is_sentinel

from . import _pydantic_builder
from ._protocol import SpecAdapter

__all__ = ("PydanticSpecAdapter",)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

    from lionagi.ln.types import Operable, Spec


# Shared across identical constructions — callers must not mutate a returned model class.
# LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0 disables sharing entirely.
_model_type_cache: BoundedLRUCache[Any, type[BaseModel]] = BoundedLRUCache(
    "LIONAGI_OPERATIVE_MODEL_CACHE_SIZE", 512
)

_lazy_field_params = LazyInit()
_PYDANTIC_FIELD_PARAMS: frozenset[str] = frozenset()


def _init_pydantic_field_params() -> None:
    global _PYDANTIC_FIELD_PARAMS
    import inspect

    from pydantic import Field as PydanticField

    parameters = set(inspect.signature(PydanticField).parameters)
    parameters.discard("kwargs")
    _PYDANTIC_FIELD_PARAMS = frozenset(parameters)


def _pydantic_field_params() -> frozenset[str]:
    _lazy_field_params.ensure(_init_pydantic_field_params)
    return _PYDANTIC_FIELD_PARAMS


def _pydantic_annotation(spec: Spec) -> Any:
    """Resolve target annotation without double-wrapping an existing list."""
    annotation: Any = Any if is_sentinel(spec.base_type) else spec.base_type
    if spec.is_listable and get_origin(annotation) is not list:
        annotation = list[annotation]
    if spec.is_nullable:
        annotation = annotation | None
    return annotation


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
        from pydantic import Field as PydanticField

        field_kwargs: dict[str, Any] = {}
        pydantic_params = _pydantic_field_params()
        consumed = {
            CommonMeta.NAME.value,
            CommonMeta.NULLABLE.value,
            CommonMeta.LISTABLE.value,
            CommonMeta.VALIDATOR.value,
        }

        for meta in spec.metadata:
            if meta.key == CommonMeta.DEFAULT.value:
                if callable(meta.value):
                    field_kwargs[CommonMeta.DEFAULT_FACTORY.value] = meta.value
                else:
                    field_kwargs[CommonMeta.DEFAULT.value] = meta.value
            elif meta.key in consumed:
                continue
            elif meta.key in pydantic_params:
                field_kwargs[meta.key] = meta.value
            elif not isinstance(meta.value, type):
                extra = field_kwargs.setdefault("json_schema_extra", {})
                if isinstance(extra, dict):
                    extra[meta.key] = meta.value

        if (
            spec.is_nullable
            and CommonMeta.DEFAULT.value not in field_kwargs
            and CommonMeta.DEFAULT_FACTORY.value not in field_kwargs
        ):
            field_kwargs[CommonMeta.DEFAULT.value] = None

        field_info = PydanticField(**field_kwargs)
        field_info.annotation = _pydantic_annotation(spec)
        return field_info

    @classmethod
    def create_validator(cls, spec: Spec) -> dict | None:
        """Create Pydantic field_validator from Spec metadata."""
        v = spec.get("validator")
        if is_sentinel(v):
            return None

        from pydantic import field_validator

        field_name = spec.name if isinstance(spec.name, str) else "field"
        validators = v if isinstance(v, list) else [v]
        suffixes = range(len(validators)) if len(validators) > 1 else (None,)
        output = {}
        for suffix, validator in zip(suffixes, validators, strict=True):
            key = (
                f"{field_name}_validator" if suffix is None else f"{field_name}_validator_{suffix}"
            )
            output[key] = field_validator(field_name)(validator)
        return output

    @classmethod
    def materialize(
        cls,
        declaration: Operable,
        /,
        *,
        model_name: str,
        include: Collection[str] | None = None,
        exclude: Collection[str] | None = None,
        base_type: type[BaseModel] | None = None,
        doc: str | None = None,
    ) -> type[BaseModel]:
        """Materialize an ordered neutral declaration as a Pydantic model class."""
        use_specs = declaration.get_specs(include=include, exclude=exclude)
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
            declaration=(declaration if use_specs is declaration.__op_fields__ else use_specs),
            doc=doc,
        )

        def build() -> type[BaseModel]:
            use_fields = {cast(str, spec.name): cls.create_field(spec) for spec in use_specs}
            validators = {}
            for spec in use_specs:
                validator = cls.create_validator(spec)
                if validator:
                    validators.update(validator)

            result = _pydantic_builder._build_pydantic_model(
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
    def create_model(
        cls,
        op: Operable,
        model_name: str,
        include: Collection[str] | None = None,
        exclude: Collection[str] | None = None,
        base_type: type[BaseModel] | None = None,
        doc: str | None = None,
    ) -> type[BaseModel]:
        """Compatibility alias for :meth:`materialize`."""
        import warnings

        warnings.warn(
            "PydanticSpecAdapter.create_model() is deprecated; use materialize() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return cls.materialize(
            op,
            model_name=model_name,
            include=include,
            exclude=exclude,
            base_type=base_type,
            doc=doc,
        )

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
