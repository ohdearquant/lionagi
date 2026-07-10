# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Runtime-checkable ObservableProto for V1; alias Observable; retains legacy nominal ABC."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Do NOT remove: Pile and others rely on issubclass(..., Observable) nominal checks.
from ._concepts import Observable as LegacyObservable

__all__ = (
    "ObservableProto",
    "Observable",
    "LegacyObservable",
)


@runtime_checkable
class ObservableProto(Protocol):
    """Structural protocol: any object with an `id` property is Observable."""

    @property
    def id(self) -> object:
        """Unique identifier."""
        ...


# Convenience alias for V1 consumers (keeps import names short)
Observable = ObservableProto
