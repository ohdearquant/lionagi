"""Field model implementation for compositional field definitions.

This module provides FieldModel, a Params-based class that enables
compositional field definitions with lazy materialization and aggressive caching.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

from typing_extensions import Self, override

from .._errors import ValidationError
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
    """Get valid Pydantic Field parameters (cached, thread-safe)."""
    _lazy_field_params.ensure(_init_pydantic_field_params)
    return _PYDANTIC_FIELD_PARAMS


# Global cache for annotated types with bounded size
_MAX_CACHE_SIZE = int(os.environ.get("LIONAGI_FIELD_CACHE_SIZE", "10000"))
_annotated_cache: OrderedDict[tuple[type, tuple[Meta, ...]], type] = OrderedDict()
_cache_lock = threading.RLock()  # Thread-safe access to cache

# Configurable limit on metadata items to prevent explosion
METADATA_LIMIT = int(os.environ.get("LIONAGI_FIELD_META_LIMIT", "10"))


@dataclass(slots=True, frozen=True, init=False)
class FieldModel(Params):
    """Field model for compositional field definitions.

    This class provides a way to define field models that can be composed
    and materialized lazily with aggressive caching for performance.

    Key features:
    - All unspecified fields are explicitly Unset (not None or empty)
    - No silent type conversions - fails fast on incorrect types
    - Aggressive caching of materialized types with LRU eviction
    - Thread-safe field creation and caching
    - Not directly instantiable - requires keyword arguments

    Attributes:
        base_type: The base Python type for this field
        metadata: Tuple of metadata to attach via Annotated

    Environment Variables:
        LIONAGI_FIELD_CACHE_SIZE: Maximum number of cached annotated types (default: 10000)
        LIONAGI_FIELD_META_LIMIT: Maximum metadata items per template (default: 10)

    Example:
        >>> field = FieldModel(base_type=str, name="username")
        >>> nullable_field = field.as_nullable()
        >>> annotated_type = nullable_field.annotated()
    """

    # Class configuration - let Params handle Unset population
    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=True, none_as_sentinel=True)

    # Public fields (all start as Unset when not provided)
    base_type: type[Any]
    metadata: tuple[Meta, ...]

    def __init__(self, base_type: type[Any] = None, **kwargs: Any) -> None:
        """Initialize FieldModel with legacy compatibility.

        Handles backward compatibility by converting old-style kwargs to the new
        Params-based format.

        Args:
            **kwargs: Arbitrary keyword arguments, including legacy ones
        """
        # Convert legacy kwargs to proper format
        if base_type is not None:
            kwargs["base_type"] = base_type
        converted = self._convert_kwargs_to_params(**kwargs)

        # Set fields directly and validate
        for k, v in converted.items():
            if k in self.allowed():
                object.__setattr__(self, k, v)
            else:
                raise ValueError(f"Invalid parameter: {k}")

        # Validate after setting all attributes
        self._validate()

    def _validate(self) -> None:
        """Validate field configuration and process metadata.

        This method performs minimal domain-specific validation, then processes
        and validates the metadata configuration.

        Raises:
            ValueError: If base_type is invalid or metadata is malformed
        """
        # Let parent handle basic Unset population
        Params._validate(self)

        # Minimal domain validation - only check what matters
        if not self._is_sentinel(self.base_type):
            # Allow types, GenericAlias (like list[str]), and union types (like str | None)
            # Check for type, generic types, or union types
            import types

            is_valid_type = (
                isinstance(self.base_type, type)
                or hasattr(self.base_type, "__origin__")
                or isinstance(
                    self.base_type, types.UnionType
                )  # Python 3.10+ union types (str | None)
                or str(type(self.base_type)) == "<class 'types.UnionType'>"  # Fallback check
            )
            if not is_valid_type:
                raise ValueError(
                    f"base_type must be a type or type annotation, got {self.base_type}"
                )

        # Validate metadata limit
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
        """Convert legacy kwargs to Params-compatible format.

        This handles backward compatibility with the old FieldModel API.

        Args:
            **kwargs: Legacy keyword arguments

        Returns:
            Dictionary of converted parameters
        """
        params = {}

        # Handle annotation alias for base_type
        if "annotation" in kwargs and "base_type" not in kwargs:
            kwargs["base_type"] = kwargs.pop("annotation")

        # "field" is a legacy alias for "name"
        if "field" in kwargs and "name" not in kwargs:
            kwargs["name"] = kwargs.pop("field")

        # Take structural fields into params directly (not metadata)
        if "base_type" in kwargs:
            params["base_type"] = kwargs.pop("base_type")
        if "metadata" in kwargs:
            params["metadata"] = tuple(kwargs.pop("metadata"))

        metadata = list(params.get("metadata", ()))

        # Handle name in metadata
        if "name" in kwargs:
            name = kwargs.pop("name")
            if name != "field":  # Only add if non-default
                metadata.append(Meta("name", name))

        # Handle special flags
        if kwargs.pop("nullable", False):
            metadata.append(Meta("nullable", True))
        if kwargs.pop("listable", False):
            metadata.append(Meta("listable", True))

        # Validate conflicting defaults
        if "default" in kwargs and "default_factory" in kwargs:
            raise ValueError("Cannot have both default and default_factory")

        # Validate validators if provided
        if "validator" in kwargs:
            validator = kwargs["validator"]
            if not callable(validator) and not (
                isinstance(validator, list) and all(callable(v) for v in validator)
            ):
                raise ValueError("Validators must be a list of functions or a function")

        # Remaining kwargs become metadata entries (permissive legacy API).
        for key, value in kwargs.items():
            metadata.append(Meta(key, value))

        if metadata:
            params["metadata"] = tuple(metadata)

        return params

    def __getattr__(self, name: str) -> Any:
        """Handle access to custom attributes stored in metadata."""
        # Use object.__getattribute__ to avoid recursion when the
        # metadata slot itself is not yet assigned (during __init__).
        try:
            metadata = object.__getattribute__(self, "metadata")
        except AttributeError:
            metadata = None

        if metadata is not None and not self._is_sentinel(metadata):
            for meta in metadata:
                if meta.key == name:
                    return meta.value

        # Special handling for common attributes with defaults
        if name == "name":
            return "field"

        # If not found, raise AttributeError as usual
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # ---- factory helpers -------------------------------------------------- #

    def as_nullable(self) -> Self:
        """Create a new field model that allows None values.

        Returns:
            New FieldModel with nullable metadata added
        """
        # Add nullable marker to metadata
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        new_metadata = (*current_metadata, Meta("nullable", True))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def as_listable(self) -> Self:
        """Create a new field model that wraps the type in a list.

        Note: This produces list[T] which is a types.GenericAlias in Python 3.11+,
        not typing.List. This is intentional for better performance and native support.

        Returns:
            New FieldModel with list wrapper
        """
        # Get current base type
        current_base = Any if self._is_sentinel(self.base_type) else self.base_type
        # Change base type to list of current type
        new_base = list[current_base]  # type: ignore
        # Add listable marker to metadata
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        new_metadata = (*current_metadata, Meta("listable", True))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", new_base)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_validator(self, f: Callable[[Any], bool]) -> Self:
        """Add a validator function to this field model.

        Args:
            f: Validator function that takes a value and returns bool

        Returns:
            New FieldModel with validator added
        """
        # Add validator to metadata
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        new_metadata = (*current_metadata, Meta("validator", f))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_description(self, description: str) -> Self:
        """Add a description to this field model.

        Args:
            description: Human-readable description of the field

        Returns:
            New FieldModel with description added
        """
        # Remove any existing description
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "description")
        new_metadata = (
            *filtered_metadata,
            Meta("description", description),
        )

        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_default(self, default: Any) -> Self:
        """Add a default value to this field model.

        Args:
            default: Default value for the field

        Returns:
            New FieldModel with default added
        """
        # Remove any existing default metadata to avoid conflicts
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "default")
        new_metadata = (*filtered_metadata, Meta("default", default))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_frozen(self, frozen: bool = True) -> Self:
        """Mark this field as frozen (immutable after creation).

        Args:
            frozen: Whether the field should be frozen

        Returns:
            New FieldModel with frozen setting
        """
        # Remove any existing frozen metadata
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "frozen")
        new_metadata = (*filtered_metadata, Meta("frozen", frozen))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_alias(self, alias: str) -> Self:
        """Add an alias to this field.

        Args:
            alias: Alternative name for the field

        Returns:
            New FieldModel with alias
        """
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "alias")
        new_metadata = (*filtered_metadata, Meta("alias", alias))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_title(self, title: str) -> Self:
        """Add a title to this field.

        Args:
            title: Human-readable title for the field

        Returns:
            New FieldModel with title
        """
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "title")
        new_metadata = (*filtered_metadata, Meta("title", title))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_exclude(self, exclude: bool = True) -> Self:
        """Mark this field to be excluded from serialization.

        Args:
            exclude: Whether to exclude the field

        Returns:
            New FieldModel with exclude setting
        """
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "exclude")
        new_metadata = (*filtered_metadata, Meta("exclude", exclude))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_metadata(self, key: str, value: Any) -> Self:
        """Add custom metadata to this field.

        Args:
            key: Metadata key
            value: Metadata value

        Returns:
            New FieldModel with custom metadata
        """
        # Replace existing metadata with same key
        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != key)
        new_metadata = (*filtered_metadata, Meta(key, value))
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def with_json_schema_extra(self, **kwargs: Any) -> Self:
        """Add JSON schema extra information.

        Args:
            **kwargs: Key-value pairs for json_schema_extra

        Returns:
            New FieldModel with json_schema_extra
        """
        # Get existing json_schema_extra or create new dict
        existing = self.extract_metadata("json_schema_extra") or {}
        updated = {**existing, **kwargs}

        current_metadata = () if self._is_sentinel(self.metadata) else self.metadata
        filtered_metadata = tuple(m for m in current_metadata if m.key != "json_schema_extra")
        new_metadata = (
            *filtered_metadata,
            Meta("json_schema_extra", updated),
        )
        # Create new instance directly without going through __init__
        new_instance = object.__new__(type(self))
        object.__setattr__(new_instance, "base_type", self.base_type)
        object.__setattr__(new_instance, "metadata", new_metadata)
        new_instance._validate()
        return new_instance

    def create_field(self) -> Any:
        """Create a Pydantic FieldInfo object from this template.

        Returns:
            A Pydantic FieldInfo object with all metadata applied
        """
        from pydantic import Field as PydanticField

        # Get valid Pydantic Field parameters (cached)
        pydantic_field_params = _get_pydantic_field_params()

        # Extract metadata for FieldInfo
        field_kwargs = {}

        if not self._is_sentinel(self.metadata):
            for meta in self.metadata:
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
                    # to avoid Pydantic serialization errors when generating JSON schema

                    # Skip model classes and other unserializable types
                    if isinstance(meta.value, type):
                        # Skip type objects (including model classes) - they can't be serialized
                        continue

                    # Any other metadata goes in json_schema_extra
                    if "json_schema_extra" not in field_kwargs:
                        field_kwargs["json_schema_extra"] = {}
                    field_kwargs["json_schema_extra"][meta.key] = meta.value

        # Handle nullable case - ensure default is set if not already
        if (
            self.is_nullable
            and "default" not in field_kwargs
            and "default_factory" not in field_kwargs
        ):
            field_kwargs["default"] = None

        field_info = PydanticField(**field_kwargs)

        # Set the annotation from base_type for backward compatibility
        field_info.annotation = self.annotation

        return field_info

    # ---- materialization -------------------------------------------------- #

    def annotated(self) -> type[Any]:
        """Materialize this template into an Annotated type.

        This method is cached to ensure repeated calls return the same
        type object for performance and identity checks. The cache is bounded
        using LRU eviction to prevent unbounded memory growth.

        Returns:
            Annotated type with all metadata attached
        """
        # Check cache first with thread safety
        cache_key = (self.base_type, self.metadata)

        with _cache_lock:
            if cache_key in _annotated_cache:
                # Move to end to mark as recently used
                _annotated_cache.move_to_end(cache_key)
                return _annotated_cache[cache_key]

            # Handle nullable case - wrap in Optional-like union
            actual_type = Any if self._is_sentinel(self.base_type) else self.base_type
            current_metadata = () if self._is_sentinel(self.metadata) else self.metadata

            if any(m.key == "nullable" and m.value for m in current_metadata):
                # Use union syntax for nullable
                actual_type = actual_type | None  # type: ignore

            if current_metadata:
                # Python 3.10 doesn't support unpacking in Annotated, so we need to build it differently
                # We'll use Annotated.__class_getitem__ to build the type dynamically
                args = [actual_type] + list(current_metadata)
                result = Annotated.__class_getitem__(tuple(args))  # type: ignore
            else:
                result = actual_type  # type: ignore[misc]

            # Cache the result with LRU eviction
            _annotated_cache[cache_key] = result  # type: ignore[assignment]

            # Evict oldest if cache is too large (guard against empty cache)
            while len(_annotated_cache) > _MAX_CACHE_SIZE:
                try:
                    _annotated_cache.popitem(last=False)  # Remove oldest
                except KeyError:
                    # Cache became empty during race, safe to continue
                    break

        return result  # type: ignore[return-value]

    def extract_metadata(self, key: str) -> Any:
        """Extract metadata value by key.

        Args:
            key: Metadata key to look for

        Returns:
            Metadata value if found, None otherwise
        """
        if not self._is_sentinel(self.metadata):
            for m in self.metadata:
                if m.key == key:
                    return m.value
        return None

    def has_validator(self) -> bool:
        """Check if this template has a validator.

        Returns:
            True if validator exists in metadata
        """
        if self._is_sentinel(self.metadata):
            return False
        return any(m.key == "validator" for m in self.metadata)

    def is_valid(self, value: Any) -> bool:
        """Check if a value is valid against all validators in this template.

        Args:
            value: Value to validate

        Returns:
            True if all validators pass, False otherwise
        """
        if self._is_sentinel(self.metadata):
            return True
        for m in self.metadata:
            if m.key == "validator":
                validator = m.value
                if not validator(value):
                    return False
        return True

    def validate(self, value: Any, field_name: str | None = None) -> None:
        """Validate a value against all validators, raising ValidationError on failure.

        Args:
            value: Value to validate
            field_name: Optional field name for error context

        Raises:
            ValidationError: If any validator fails
        """
        # Early exit if no validators
        if not self.has_validator():
            return

        if not self._is_sentinel(self.metadata):
            for i, m in enumerate(self.metadata):
                if m.key == "validator":
                    validator = m.value
                    # Try to call validator with correct signature
                    try:
                        # Try Pydantic-style validator (cls, value) - pass None for cls
                        result = validator(None, value)
                        # For Pydantic validators that return the value or raise exceptions,
                        # if we get here without exception, validation passed
                    except TypeError:
                        # Try simple validator that just takes value and returns boolean
                        result = validator(value)
                        # If validator returns False (simple boolean validator), raise error
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
                        # If validator raises any other exception, let it propagate
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
        """String representation of the field model."""
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
        """Create field validator configuration for backward compatibility.

        Returns:
            Dictionary mapping validator name to validator function if defined,
            None otherwise.
        """
        if not self.has_validator():
            return None

        # Extract validators and create field_validator config
        from pydantic import field_validator

        validators = {}

        # Get field name from metadata or use default
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
        """Convert FieldModel to Spec.

        Returns:
            Spec object with equivalent configuration
        """
        from ..ln.types import Spec

        # Build kwargs for Spec constructor
        kwargs = {}

        # Extract name from metadata
        name = self.extract_metadata("name")
        if name:
            kwargs["name"] = name

        # Add nullable/listable flags using properties
        kwargs["nullable"] = self.is_nullable
        kwargs["listable"] = self.is_listable

        # Extract default/default_factory
        default = self.extract_metadata("default")
        if default is not None:
            kwargs["default"] = default

        default_factory = self.extract_metadata("default_factory")
        if default_factory is not None:
            kwargs["default_factory"] = default_factory

        # Extract validator
        validator = self.extract_metadata("validator")
        if validator is not None:
            kwargs["validator"] = validator

        # Extract description
        description = self.extract_metadata("description")
        if description:
            kwargs["description"] = description

        # Extract other common metadata
        for key in ["title", "alias", "frozen", "exclude"]:
            val = self.extract_metadata(key)
            if val is not None:
                kwargs[key] = val

        # Extract json_schema_extra
        json_schema_extra = self.extract_metadata("json_schema_extra")
        if json_schema_extra:
            for k, v in json_schema_extra.items():
                kwargs[k] = v

        return Spec(self.base_type, **kwargs)

    def metadata_dict(self, exclude: list[str] | None = None) -> dict[str, Any]:
        """Convert all metadata to dictionary with optional exclusions.

        Args:
            exclude: List of metadata keys to exclude from the result

        Returns:
            Dictionary mapping metadata keys to their values
        """
        result = {}
        exclude_set = set(exclude or [])

        # Convert metadata to dictionary
        if not self._is_sentinel(self.metadata):
            for meta in self.metadata:
                if meta.key not in exclude_set:
                    result[meta.key] = meta.value

        return result


__all__ = ("FieldModel",)
