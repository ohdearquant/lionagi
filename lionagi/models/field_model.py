"""Compositional field definitions with lazy materialization."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from typing_extensions import Self, override

from .._errors import ValidationError
from ..ln.types import MaybeSentinel, Meta, ModelConfig, Params, Spec, Undefined, Unset
from ..ln.types._annotation import _materialize_annotation

METADATA_LIMIT = int(os.environ.get("LIONAGI_FIELD_META_LIMIT", "10"))


@dataclass(slots=True, frozen=True, init=False, eq=False)
class FieldModel(Params):
    """Compositional field definition with lazy Annotated-type materialization."""

    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=True, none_as_sentinel=True)

    base_type: MaybeSentinel[type[Any]] | None
    metadata: tuple[Meta, ...]

    def __init__(
        self,
        base_type: MaybeSentinel[type[Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        import warnings

        warnings.warn(
            "FieldModel is deprecated as a declaration authority; use Spec instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if base_type is not None:
            kwargs["base_type"] = base_type
        converted = self._convert_kwargs_to_params(**kwargs)
        Params.__init__(self, **converted)

    def _validate(self) -> None:
        Params._validate(self)

        if not self._is_sentinel(self.base_type):
            import types

            is_valid_type = (
                isinstance(self.base_type, type)
                or hasattr(self.base_type, "__origin__")
                or isinstance(
                    self.base_type, types.UnionType
                )  # Python 3.10+ union types (str | None)
            )
            if not is_valid_type:
                raise ValueError(
                    f"base_type must be a type or type annotation, got {self.base_type}"
                )

        if not self._is_sentinel(self.metadata):
            if len(self.metadata) > METADATA_LIMIT:
                import warnings

                warnings.warn(
                    f"FieldModel has {len(self.metadata)} metadata items, "
                    f"exceeding recommended limit of {METADATA_LIMIT}. "
                    "Consider simplifying the field definition.",
                    stacklevel=3,
                )

    @classmethod
    def _convert_kwargs_to_params(cls, **kwargs: Any) -> dict[str, Any]:
        """Convert legacy kwargs to Params-compatible format."""
        params = {}

        # "annotation" is a legacy alias for "base_type"
        if "annotation" in kwargs and "base_type" not in kwargs:
            kwargs["base_type"] = kwargs.pop("annotation")

        if "field" in kwargs and "name" not in kwargs:
            kwargs["name"] = kwargs.pop("field")

        if "base_type" in kwargs:
            params["base_type"] = kwargs.pop("base_type")
        if "metadata" in kwargs:
            raw_metadata = kwargs.pop("metadata")
            params["metadata"] = (
                raw_metadata
                if raw_metadata is Undefined or raw_metadata is Unset
                else tuple(raw_metadata)
            )

        current_metadata = params.get("metadata", ())
        metadata = (
            []
            if current_metadata is Undefined or current_metadata is Unset
            else list(current_metadata)
        )

        if "name" in kwargs:
            name = kwargs.pop("name")
            if name != "field":  # Only add if non-default
                metadata.append(Meta("name", name))

        if kwargs.pop("nullable", False):
            metadata.append(Meta("nullable", True))
        if kwargs.pop("listable", False):
            metadata.append(Meta("listable", True))

        if "default" in kwargs and "default_factory" in kwargs:
            raise ValueError("Cannot have both default and default_factory")

        if "validator" in kwargs:
            validator = kwargs["validator"]
            if not callable(validator) and not (
                isinstance(validator, list) and all(callable(v) for v in validator)
            ):
                raise ValueError("Validators must be a list of functions or a function")

        for key, value in kwargs.items():
            metadata.append(Meta(key, value))

        if metadata:
            params["metadata"] = tuple(metadata)

        return params

    def __getattr__(self, name: str) -> Any:
        # Avoid recursion when metadata slot is not yet assigned (during __init__)
        try:
            metadata = object.__getattribute__(self, "metadata")
        except AttributeError:
            metadata = None

        if metadata is not None and not self._is_sentinel(metadata):
            for meta in metadata:
                if meta.key == name:
                    return meta.value

        if name == "name":
            return "field"

        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # ---- factory helpers -------------------------------------------------- #

    def as_nullable(self) -> Self:
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        new_metadata = (*current_metadata, Meta("nullable", True))
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def as_listable(self) -> Self:
        current_base = Any if self._is_sentinel(self.base_type) else self.base_type
        new_base = list[current_base]  # type: ignore
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        new_metadata = (*current_metadata, Meta("listable", True))
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", new_base)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_validator(self, f: Callable[[Any], bool]) -> Self:
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        new_metadata = (*current_metadata, Meta("validator", f))
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_description(self, description: str) -> Self:
        return self.with_metadata("description", description)

    def with_default(self, default: Any) -> Self:
        return self.with_metadata("default", default)

    def with_frozen(self, frozen: bool = True) -> Self:
        return self.with_metadata("frozen", frozen)

    def with_alias(self, alias: str) -> Self:
        return self.with_metadata("alias", alias)

    def with_title(self, title: str) -> Self:
        return self.with_metadata("title", title)

    def with_exclude(self, exclude: bool = True) -> Self:
        return self.with_metadata("exclude", exclude)

    def with_metadata(self, key: str, value: Any) -> Self:
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != key)
        new_metadata = (*filtered_metadata, Meta(key, value))
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_json_schema_extra(self, **kwargs: Any) -> Self:
        existing = self.extract_metadata("json_schema_extra") or {}
        updated = {**existing, **kwargs}

        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "json_schema_extra")
        new_metadata = (
            *filtered_metadata,
            Meta("json_schema_extra", updated),
        )
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def create_field(self) -> Any:
        """Create a Pydantic FieldInfo through the neutral declaration adapter."""
        from lionagi.adapters.spec_adapters import PydanticSpecAdapter

        return PydanticSpecAdapter.create_field(self.to_spec())

    # ---- materialization -------------------------------------------------- #

    def annotated(self) -> type[Any]:
        """Materialize through the shared identity-safe annotation cache."""
        return _materialize_annotation(
            owner=self,
            base_type=self.base_type,
            metadata=self.metadata,
            sentinel_predicate=self._is_sentinel,
        )

    def extract_metadata(self, key: str) -> Any:
        if not self._is_sentinel(self.metadata):
            for m in self.metadata:
                if m.key == key:
                    return m.value
        return None

    def has_validator(self) -> bool:
        if self._is_sentinel(self.metadata):
            return False
        return any(m.key == "validator" for m in self.metadata)

    def is_valid(self, value: Any) -> bool:
        if self._is_sentinel(self.metadata):
            return True
        for m in self.metadata:
            if m.key == "validator":
                validator = m.value
                if not validator(value):
                    return False
        return True

    def validate(self, value: Any, field_name: str | None = None) -> None:
        if not self.has_validator():
            return

        if not self._is_sentinel(self.metadata):
            for i, m in enumerate(self.metadata):
                if m.key == "validator":
                    validator = m.value
                    try:
                        # Try Pydantic-style validator (cls, value)
                        result = validator(None, value)
                    except TypeError:
                        # Fall back to simple validator(value) -> bool
                        result = validator(value)
                        if result is False:
                            validator_name = getattr(validator, "__name__", f"validator_{i}")
                            raise ValidationError(
                                f"Validation failed for {validator_name}",
                                details={
                                    "field_name": field_name,
                                    "value": value,
                                    "validator_name": validator_name,
                                },
                            ) from None
                    except Exception:
                        raise

    @property
    def is_nullable(self) -> bool:
        """Check if this field allows None values."""
        if self._is_sentinel(self.metadata):
            return False
        return any(m.key == "nullable" and m.value for m in self.metadata)

    @property
    def is_listable(self) -> bool:
        """Check if this field is a list type."""
        if self._is_sentinel(self.metadata):
            return False
        return any(m.key == "listable" and m.value for m in self.metadata)

    @override
    def __repr__(self) -> str:
        import types

        attrs = []
        if self.is_nullable:
            attrs.append("nullable")
        if self.is_listable:
            attrs.append("listable")
        if self.has_validator():
            attrs.append("validated")

        attr_str = f" [{', '.join(attrs)}]" if attrs else ""
        if self._is_sentinel(self.base_type):
            base_type_name = "Any"
        elif isinstance(self.base_type, types.UnionType):
            base_type_name = str(self.base_type)
        else:
            base_type_name = getattr(self.base_type, "__name__", str(self.base_type))
        return f"FieldModel({base_type_name}{attr_str})"

    @property
    def field_validator(self) -> dict[str, Any] | None:
        """Build compatibility validators through the target-owned adapter."""
        from lionagi.adapters.spec_adapters import PydanticSpecAdapter

        return PydanticSpecAdapter.create_validator(self.to_spec())

    @property
    def annotation(self) -> type[Any]:
        if self._is_sentinel(self.base_type):
            return Any
        t_ = cast(type[Any], self.base_type)
        if self.is_listable:
            # Avoid double-wrapping if base_type is already list[X]
            origin = getattr(t_, "__origin__", None)
            if origin is not list:
                t_ = list[t_]
        if self.is_nullable:
            t_ = t_ | None
        return t_

    def to_spec(self) -> Spec:
        # Metadata crosses as a normalized Meta tuple, not **kwargs — see
        # docs/internals/support-libs.md#modelsfield_model-fieldmodelto_spec
        existing = () if self._is_sentinel(self.metadata) else self.metadata
        # FieldModel historically accepted repeated metadata. Its Pydantic
        # materializer used the last value for ordinary keys and validators,
        # while the owning field name came from the first declaration. Collapse
        # that legacy surface here instead of weakening Spec's unique-key rule.
        normalized: dict[str, Meta] = {}
        for meta in existing:
            if meta.key in ("nullable", "listable"):
                continue
            if meta.key == "name" and meta.key in normalized:
                continue
            normalized[meta.key] = meta
        metas = list(normalized.values())
        metas.append(Meta("nullable", self.is_nullable))
        metas.append(Meta("listable", self.is_listable))

        # FieldModel historically accepts annotation=None as an unspecified
        # adapter input. Keep that boundary explicit instead of teaching Spec
        # to collapse a caller-supplied null into absence.
        base_type = Unset if self.base_type is None else self.base_type
        return Spec(base_type, metadata=tuple(metas))

    def metadata_dict(self, exclude: list[str] | None = None) -> dict[str, Any]:
        result = {}
        exclude_set = set(exclude or [])
        if not self._is_sentinel(self.metadata):
            for meta in self.metadata:
                if meta.key not in exclude_set:
                    result[meta.key] = meta.value

        return result


__all__ = ("FieldModel",)
