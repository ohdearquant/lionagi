# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Rendering and parsing protocol types for the message layer."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from lionagi.ln.types import Enum

__all__ = ("CustomRenderer", "CustomParser", "StructureFormat")


@runtime_checkable
class CustomRenderer(Protocol):
    """Protocol for custom instruction renderers: (model, **kwargs) -> str."""

    def __call__(self, model: type[BaseModel], **kwargs: Any) -> str: ...


@runtime_checkable
class CustomParser(Protocol):
    """Protocol for custom output parsers (e.g., LNDL): (text, target_keys, **kwargs) -> dict."""

    def __call__(self, text: str, target_keys: list[str], **kwargs: Any) -> dict[str, Any]: ...


class StructureFormat(Enum):
    """Enumeration of structure formats for instruction rendering."""

    JSON = "json"
    CUSTOM = "custom"
    LNDL = "lndl"
