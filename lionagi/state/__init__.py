# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .compat import CompatibilityProfile, StateCompatibilityRefused
    from .db import (
        ForeignLineageError,
        SchemaTooNewError,
        StateDB,
        UnreadableStoreError,
        UnrecognizedStoreError,
    )

__all__ = (
    "CompatibilityProfile",
    "ForeignLineageError",
    "SchemaTooNewError",
    "StateCompatibilityRefused",
    "StateDB",
    "UnreadableStoreError",
    "UnrecognizedStoreError",
)

_MODULES = {
    "CompatibilityProfile": "compat",
    "ForeignLineageError": "db",
    "SchemaTooNewError": "db",
    "StateCompatibilityRefused": "compat",
    "StateDB": "db",
    "UnreadableStoreError": "db",
    "UnrecognizedStoreError": "db",
}


def __getattr__(name: str):
    if name in __all__:
        from importlib import import_module

        value = getattr(import_module(f".{_MODULES[name]}", __name__), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
