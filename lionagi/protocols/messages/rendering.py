# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Opt-in rendering and parsing protocol types for manual message pipelines.

``CustomRenderer`` and ``CustomParser`` are structural contracts for caller-owned
integrations. Branch operations do not discover or invoke them. Use
``prepare_messages_for_chat`` explicitly to compile branch history, add the
renderer output to that provider request, and apply the parser to the returned
text. See ``docs/api/operations.md`` for the supported opt-in path.
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from lionagi.ln.types import Enum

__all__ = ("CustomRenderer", "CustomParser", "StructureFormat")


@runtime_checkable
class CustomRenderer(Protocol):
    """Caller-invoked protocol for rendering a model schema as instruction text."""

    def __call__(self, model: type[BaseModel], **kwargs: Any) -> str: ...


@runtime_checkable
class CustomParser(Protocol):
    """Caller-invoked protocol for parsing text into named output fields."""

    def __call__(self, text: str, target_keys: list[str], **kwargs: Any) -> dict[str, Any]: ...


class StructureFormat(Enum):
    """Enumeration of structure formats for instruction rendering."""

    JSON = "json"
    CUSTOM = "custom"
    LNDL = "lndl"
