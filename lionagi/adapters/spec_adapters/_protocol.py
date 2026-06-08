# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Abstract base class for Spec adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.ln.types import Operable, Spec

__all__ = ("SpecAdapter",)


class SpecAdapter(ABC):
    """Base adapter for converting Spec to framework-specific formats."""

    @classmethod
    @abstractmethod
    def create_field(cls, spec: Spec) -> Any:
        """Convert Spec to framework-specific field definition."""
        ...

    @classmethod
    @abstractmethod
    def create_model(
        cls,
        operable: Operable,
        model_name: str,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        **kwargs: Any,
    ) -> type:
        """Generate model class from Operable."""
        ...

    @classmethod
    @abstractmethod
    def validate_model(cls, model_cls: type, data: dict) -> Any:
        """Validate dict data into model instance."""
        ...

    @classmethod
    @abstractmethod
    def dump_model(cls, instance: Any) -> dict:
        """Dump model instance to dictionary."""
        ...

    @classmethod
    def create_validator(cls, spec: Spec) -> Any:
        """Generate framework-specific validators from Spec metadata."""
        return None

    @classmethod
    def parse_json(cls, text: str, fuzzy: bool = True) -> dict | list | Any:
        """Extract and parse JSON from text."""
        from lionagi.ln import extract_json

        data = extract_json(text, fuzzy_parse=fuzzy)

        # Unwrap single-item lists/tuples
        if isinstance(data, list | tuple) and len(data) == 1:
            data = data[0]

        return data

    @classmethod
    @abstractmethod
    def fuzzy_match_fields(cls, data: dict, model_cls: type, strict: bool = False) -> dict:
        """Match data keys to model fields with fuzzy matching."""
        ...

    @classmethod
    def validate_response(
        cls,
        text: str,
        model_cls: type,
        strict: bool = False,
        fuzzy_parse: bool = True,
    ) -> Any | None:
        """Parse response text into validated model instance."""
        try:
            # Step 1: Parse JSON
            data = cls.parse_json(text, fuzzy=fuzzy_parse)

            # Step 2: Fuzzy match fields
            matched_data = cls.fuzzy_match_fields(data, model_cls, strict=strict)

            # Step 3: Validate with framework-specific method
            instance = cls.validate_model(model_cls, matched_data)

            return instance

        except (ValueError, TypeError, KeyError, AttributeError):
            if strict:
                raise
            return None

    @classmethod
    def update_model(
        cls,
        instance: Any,
        updates: dict,
        model_cls: type | None = None,
    ) -> Any:
        """Update existing model instance with new data."""
        model_cls = model_cls or type(instance)

        # Merge existing data with updates
        current_data = cls.dump_model(instance)
        current_data.update(updates)

        # Validate merged data
        return cls.validate_model(model_cls, current_data)
