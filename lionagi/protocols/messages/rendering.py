# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Rendering and parsing protocol types for the message layer."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from lionagi.ln.types import Enum

__all__ = ("CustomRenderer", "CustomParser", "StructureFormat")


@runtime_checkable
class CustomRenderer(Protocol):
    """Protocol for custom instruction renderers.

    Implementations format request_model schema for custom output formats.

    Args:
        model: Pydantic model class defining expected response schema
        **kwargs: Additional renderer-specific options

    Returns:
        Formatted instruction string for the custom output format
    """

    def __call__(self, model: type[BaseModel], **kwargs: Any) -> str: ...


@runtime_checkable
class CustomParser(Protocol):
    """Protocol for custom output parsers (e.g., LNDL).

    Implementations extract structured data from LLM text responses.

    Args:
        text: Raw LLM response text
        target_keys: Expected field names for fuzzy matching
        **kwargs: Additional parser-specific options

    Returns:
        Dict mapping field names to extracted values
    """

    def __call__(self, text: str, target_keys: list[str], **kwargs: Any) -> dict[str, Any]: ...


class StructureFormat(Enum):
    """Enumeration of structure formats for instruction rendering."""

    JSON = "json"
    CUSTOM = "custom"
    LNDL = "lndl"
