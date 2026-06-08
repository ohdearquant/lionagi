"""Compositional field definitions with lazy materialization."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

from typing_extensions import Self, override

from .._errors import ValidationError
from ..ln._cache import BoundedLRUCache
from ..ln._lazy_init import LazyInit
from ..ln.types import Meta, ModelConfig, Params, Spec

# Cache of valid Pydantic Field parameters
_lazy_field_params = LazyInit()
_PYDANTIC_FIELD_PARAMS: set[str] = set()


def _init_pydantic_field_params() -> None:
    global _PYDANTIC_FIELD_PARAMS
    import inspect

    from pydantic import Field as PydanticField

    _PYDANTIC_FIELD_PARAMS = set(inspect.signature(PydanticField).parameters.keys())
    _PYDANTIC_FIELD_PARAMS.discard("kwargs")


def _get_pydantic_field_params() -> set[str]:
    _lazy_field_params.ensure(_init_pydantic_field_params)
    return _PYDANTIC_FIELD_PARAMS


_annotated_cache: BoundedLRUCache[tuple[type, tuple[Meta, ...]], type] = BoundedLRUCache(
    "LIONAGI_FIELD_CACHE_SIZE", 10000
)

METADATA_LIMIT = int(os.environ.get("LIONAGI_FIELD_META_LIMIT", "10"))


@dataclass(slots=True, frozen=True, init=False)
class FieldModel(Params):
    """Compositional field definition with lazy Annotated-type materialization."""

    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=True, none_as_sentinel=True)

    base_type: type[Any]
    metadata: tuple[Meta, ...]

    def __init__(self, base_type: type[Any] = None, **kwargs: Any) -> None:
        if base_type is not None:
            kwargs["base_type"] = base_type
        converted = self._convert_kwargs_to_params(**kwargs)
        for k, v in converted.items():
            if k in self.allowed():
                object.__setattr__(self, k, v)
            else:
                raise ValueError(f"Invalid parameter: {k}")

        self._validate()

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
                or str(type(self.base_type)) == "<class 'types.UnionType'>"
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
            params["metadata"] = tuple(kwargs.pop("metadata"))

        metadata = list(params.get("metadata", ()))

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
        """Create a Pydantic FieldInfo from this template."""
        from pydantic import Field as PydanticField

        pydantic_field_params = _get_pydantic_field_params()
        field_kwargs = {}

        if not self._is_sentinel(self.metadata):
            for meta in self.metadata:
                if meta.key == "default":
                    if callable(meta.value):
                        field_kwargs["default_factory"] = meta.value
                    else:
                        field_kwargs["default"] = meta.value
                elif meta.key == "validator":
                    continue
                elif meta.key in pydantic_field_params:
                    field_kwargs[meta.key] = meta.value
                elif meta.key in {"nullable", "listable"}:
                    pass
                else:
                    # Skip type objects -- unserializable in JSON schema
                    if isinstance(meta.value, type):
                        continue
                    if "json_schema_extra" not in field_kwargs:
                        field_kwargs["json_schema_extra"] = {}
                    field_kwargs["json_schema_extra"][meta.key] = meta.value

        if (
            self.is_nullable
            and "default" not in field_kwargs
            and "default_factory" not in field_kwargs
        ):
            field_kwargs["default"] = None

        field_info = PydanticField(**field_kwargs)
        field_info.annotation = self.annotation

        return field_info

    # ---- materialization -------------------------------------------------- #

    def annotated(self) -> type[Any]:
        """Materialize into an Annotated type (LRU-cached, thread-safe)."""
        cache_key = (self.base_type, self.metadata)

        cached = _annotated_cache.get(cache_key)
        if cached is not None:
            return cached

        actual_type = Any if self._is_sentinel(self.base_type) else self.base_type
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata

        if any(m.key == "nullable" and m.value for m in current_metadata):
            actual_type = actual_type | None  # type: ignore

        if current_metadata:
            args = [actual_type] + list(current_metadata)
            result = Annotated.__class_getitem__(tuple(args))  # type: ignore
        else:
            result = actual_type  # type: ignore[misc]

        _annotated_cache.put(cache_key, result)  # type: ignore[arg-type]
        return result  # type: ignore[return-value]

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
        if not self.has_validator():
            return None

        from pydantic import field_validator

        validators = {}
        field_name = self.extract_metadata("name") or "field"

        if not self._is_sentinel(self.metadata):
            for meta in self.metadata:
                if meta.key == "validator":
                    validator_name = f"{field_name}_validator"
                    validators[validator_name] = field_validator(field_name)(meta.value)

        return validators if validators else None

    @property
    def annotation(self) -> type[Any]:
        if self._is_sentinel(self.base_type):
            return Any
        t_ = self.base_type
        if self.is_listable:
            # Avoid double-wrapping if base_type is already list[X]
            origin = getattr(t_, "__origin__", None)
            if origin is not list:
                t_ = list[t_]
        if self.is_nullable:
            t_ = t_ | None
        return t_

    def to_spec(self) -> Spec:
        from ..ln.types import Spec

        kwargs = {}
        name = self.extract_metadata("name")
        if name:
            kwargs["name"] = name

        kwargs["nullable"] = self.is_nullable
        kwargs["listable"] = self.is_listable

        default = self.extract_metadata("default")
        if default is not None:
            kwargs["default"] = default

        default_factory = self.extract_metadata("default_factory")
        if default_factory is not None:
            kwargs["default_factory"] = default_factory

        validator = self.extract_metadata("validator")
        if validator is not None:
            kwargs["validator"] = validator

        description = self.extract_metadata("description")
        if description:
            kwargs["description"] = description

        for key in ["title", "alias", "frozen", "exclude"]:
            val = self.extract_metadata(key)
            if val is not None:
                kwargs[key] = val

        json_schema_extra = self.extract_metadata("json_schema_extra")
        if json_schema_extra:
            for k, v in json_schema_extra.items():
                kwargs[k] = v

        return Spec(self.base_type, **kwargs)

    def metadata_dict(self, exclude: list[str] | None = None) -> dict[str, Any]:
        result = {}
        exclude_set = set(exclude or [])
        if not self._is_sentinel(self.metadata):
            for meta in self.metadata:
                if meta.key not in exclude_set:
                    result[meta.key] = meta.value

        return result


__all__ = ("FieldModel",)
