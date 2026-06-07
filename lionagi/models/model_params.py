# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ModelParams implementation using Params base class with aggressive caching.

This module provides ModelParams, a configuration class for dynamically creating
Pydantic models with explicit behavior and aggressive caching for performance.
"""

from __future__ import annotations

import inspect
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, ClassVar

from pydantic import BaseModel, create_model
from pydantic.fields import FieldInfo

from lionagi.ln.types import ModelConfig, Params
from lionagi.utils import copy

from .field_model import FieldModel

__all__ = ("ModelParams",)

# Global cache configuration
_MODEL_CACHE_SIZE = int(os.environ.get("LIONAGI_MODEL_CACHE_SIZE", "1000"))
_model_cache: OrderedDict[int, type[BaseModel]] = OrderedDict()
_model_cache_lock = threading.RLock()


@dataclass(slots=True, frozen=True, init=False)
class ModelParams(Params):
    """Configuration for dynamically creating Pydantic models.

    This class provides a way to configure and create Pydantic models dynamically
    with explicit behavior (no silent conversions) and aggressive caching for
    performance optimization.

    Key features:
    - All unspecified fields are explicitly Unset (not None or empty)
    - No silent type conversions - fails fast on incorrect types
    - Aggressive caching of created models with LRU eviction
    - Thread-safe model creation and caching
    - Not directly instantiable - requires keyword arguments

    Attributes:
        name: Name for the generated model class
        parameter_fields: Field definitions for the model
        base_type: Base model class to inherit from
        field_models: List of FieldModel definitions
        exclude_fields: Fields to exclude from the final model
        field_descriptions: Custom descriptions for fields
        inherit_base: Whether to inherit from base_type
        config_dict: Pydantic model configuration
        doc: Docstring for the generated model
        frozen: Whether the model should be immutable

    Environment Variables:
        LIONAGI_MODEL_CACHE_SIZE: Maximum number of cached models (default: 1000)

    Examples:
        >>> params = ModelParams(
        ...     name="UserModel",
        ...     frozen=True,
        ...     field_models=[
        ...         FieldModel(str, name="username"),
        ...         FieldModel(int, name="age", default=0)
        ...     ],
        ...     doc="A user model with basic attributes."
        ... )
        >>> UserModel = params.create_new_model()

        >>> # All unspecified fields are Unset
        >>> params2 = ModelParams(name="SimpleModel")
        >>> assert params2.doc is Unset
        >>> assert params2.frozen is Unset
    """

    # Class configuration - let Params handle Unset population
    _config: ClassVar[ModelConfig] = ModelConfig(prefill_unset=True, none_as_sentinel=True)

    # Public fields (all start as Unset when not provided)
    name: str | None
    parameter_fields: dict[str, FieldInfo]
    base_type: type[BaseModel]
    field_models: list[FieldModel]
    exclude_fields: list[str]
    field_descriptions: dict[str, str]
    inherit_base: bool
    config_dict: dict[str, Any] | None
    doc: str | None
    frozen: bool

    # Private computed state
    _final_fields: dict[str, FieldInfo] = dc_field(default_factory=dict, init=False)
    _validators: dict[str, Callable] = dc_field(default_factory=dict, init=False)

    def _validate(self) -> None:
        """Validate types and setup model configuration.

        This method performs minimal domain-specific validation, then processes
        and merges field definitions from various sources.

        Raises:
            ValueError: If base_type is not a BaseModel subclass
        """
        # Let parent handle basic Unset population
        Params._validate(self)

        # Minimal domain validation - only check what matters
        if not self._is_sentinel(self.base_type):
            if not (inspect.isclass(self.base_type) and issubclass(self.base_type, BaseModel)):
                raise ValueError(f"base_type must be BaseModel subclass, got {self.base_type}")

        # Process and merge all field sources
        self._process_fields()

    def _process_fields(self) -> None:
        """Merge all field sources into final configuration.

        This method processes and combines fields from parameter_fields, base_type,
        and field_models, handling exclusions and descriptions.
        """
        fields = {}
        validators = {}

        # Start with explicit parameter_fields
        if not self._is_sentinel(self.parameter_fields):
            # Handle empty values - treat them as no fields
            if not self.parameter_fields:
                pass  # Empty dict/list/None - no fields to add
            elif isinstance(self.parameter_fields, dict):
                # Validate parameter_fields contain FieldInfo instances
                for name, field_info in self.parameter_fields.items():
                    if not isinstance(field_info, FieldInfo):
                        raise ValueError(
                            f"parameter_fields must contain FieldInfo instances, got {type(field_info)} for field '{name}'"
                        )
                fields.update(copy(self.parameter_fields))
            else:
                raise ValueError(
                    f"parameter_fields must be a dictionary, got {type(self.parameter_fields)}"
                )

        # Add base_type fields (respecting exclusions)
        if not self._is_sentinel(self.base_type):
            base_fields = copy(self.base_type.model_fields)
            if not self._is_sentinel(self.exclude_fields):
                base_fields = {k: v for k, v in base_fields.items() if k not in self.exclude_fields}
            fields.update(base_fields)

        # Process field_models
        if not self._is_sentinel(self.field_models):
            # Coerce to list if single FieldModel instance
            field_models_list = (
                [self.field_models]
                if isinstance(self.field_models, FieldModel)
                else self.field_models
            )

            for fm in field_models_list:
                if not isinstance(fm, FieldModel):
                    raise ValueError(
                        f"field_models must contain FieldModel instances, got {type(fm)}"
                    )

            # Apply descriptions first
            field_models = field_models_list
            if not self._is_sentinel(self.field_descriptions):
                field_models = [
                    (
                        fm.with_description(self.field_descriptions[fm.name])
                        if fm.name in self.field_descriptions
                        else fm
                    )
                    for fm in field_models
                ]

            # Extract fields and validators using public interface
            for fm in field_models:
                fields[fm.name] = fm.create_field()
                fields[fm.name].annotation = fm.annotation

                # Use the public field_validator property
                if fm.field_validator:
                    validators.update(fm.field_validator)

        # Store computed state
        object.__setattr__(self, "_final_fields", fields)
        object.__setattr__(self, "_validators", validators)

    @property
    def use_fields(self) -> dict[str, tuple[type, FieldInfo]]:
        """Get field definitions to use in new model.

        Filters and prepares fields based on processed configuration.

        Returns:
            Dictionary mapping field names to (type, FieldInfo) tuples
        """
        if not hasattr(self, "_final_fields"):
            return {}

        return {k: (v.annotation, v) for k, v in self._final_fields.items()}

    @property
    def _use_keys(self) -> set[str]:
        """Get field keys for backward compatibility.

        Returns the set of field names that will be used in the generated model.
        This is derived from _final_fields for consistency.
        """
        if not hasattr(self, "_final_fields"):
            return set()
        return set(self._final_fields.keys())

    def _get_cache_key(self) -> int:
        """Create a hashable cache key from object state.

        Converts unhashable types to hashable representations for caching.
        """
        state = self.to_dict()

        def make_hashable(obj):
            if isinstance(obj, dict):
                return tuple(sorted((k, make_hashable(v)) for k, v in obj.items()))
            elif isinstance(obj, list):
                return tuple(make_hashable(x) for x in obj)
            elif isinstance(obj, set):
                return tuple(sorted(make_hashable(x) for x in obj))
            else:
                return obj

        hashable_state = make_hashable(state)
        return hash(hashable_state)

    def create_new_model(self) -> type[BaseModel]:
        """Create new Pydantic model with specified configuration.

        This method generates a new Pydantic model class based on the configured
        parameters. Results are cached for performance when the same configuration
        is used multiple times.

        Returns:
            Newly created or cached Pydantic model class
        """
        # Create stable cache key from hashable representation
        cache_key = self._get_cache_key()

        # Check cache first
        with _model_cache_lock:
            if cache_key in _model_cache:
                _model_cache.move_to_end(cache_key)
                return _model_cache[cache_key]

        # Determine model name
        model_name = self.name
        if self._is_sentinel(model_name) and not self._is_sentinel(self.base_type):
            if hasattr(self.base_type, "class_name"):
                model_name = self.base_type.class_name
                if callable(model_name):
                    model_name = model_name()
            else:
                model_name = self.base_type.__name__

        if self._is_sentinel(model_name):
            model_name = "GeneratedModel"

        # Determine base class
        base_type = None
        if (
            not self._is_sentinel(self.inherit_base)
            and self.inherit_base
            and not self._is_sentinel(self.base_type)
        ):
            # Don't inherit if we're excluding base fields
            if self._is_sentinel(self.exclude_fields) or not any(
                f in self.exclude_fields for f in self.base_type.model_fields
            ):
                base_type = self.base_type

        # Create the model
        model = create_model(
            model_name,
            __base__=base_type,
            __config__=(self.config_dict if not self._is_sentinel(self.config_dict) else None),
            __doc__=self.doc if not self._is_sentinel(self.doc) else None,
            __validators__=self._validators if self._validators else None,
            **self.use_fields,
        )

        # Apply frozen configuration
        if not self._is_sentinel(self.frozen) and self.frozen:
            model.model_config["frozen"] = True

        # Cache the result
        with _model_cache_lock:
            _model_cache[cache_key] = model

            # LRU eviction
            while len(_model_cache) > _MODEL_CACHE_SIZE:
                try:
                    _model_cache.popitem(last=False)  # Remove oldest
                except KeyError:
                    # Handle race condition
                    break

        return model
