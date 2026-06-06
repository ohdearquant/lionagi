# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""V1 Observable Protocol for gradual evolution.

This module provides the runtime-checkable ObservableProto for V1 components
while maintaining compatibility with V0's nominal Observable ABC.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = (
    "ObservableProto",
    "Observable",
    "LegacyObservable",
)


@runtime_checkable
class ObservableProto(Protocol):
    """Structural Observable Protocol for V1 components.

    This protocol defines the minimal contract for observable objects:
    they must have an 'id' property. The return type is permissive (Any)
    to maintain compatibility with V0's UUID wrapper while enabling
    V1 evolution.

    All V0 Element subclasses automatically satisfy this protocol without
    any code changes, enabling zero-risk gradual migration.
    """

    @property
    def id(self) -> object:
        """Unique identifier. Accepts UUID, UUID, or string."""
        ...


# Convenience alias for V1 consumers (keeps import names short)
Observable = ObservableProto

# Keep legacy nominal ABC for places that need issubclass checks (e.g., Pile)
# Do NOT remove – Pile and others rely on issubclass(..., Observable) nominal checks.
from ._concepts import Observable as LegacyObservable  # noqa: E402  (intentional late import)
