# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

from lionagi.ln import lazy_import

if TYPE_CHECKING:
    from .compat import CompatibilityProfile, StateCompatibilityRefused
    from .db import ForeignLineageError, SchemaTooNewError, StateDB

__all__ = (
    "CompatibilityProfile",
    "ForeignLineageError",
    "SchemaTooNewError",
    "StateCompatibilityRefused",
    "StateDB",
)

_MODULES = {
    "CompatibilityProfile": ("compat", "CompatibilityProfile"),
    "ForeignLineageError": ("db", "ForeignLineageError"),
    "SchemaTooNewError": ("db", "SchemaTooNewError"),
    "StateCompatibilityRefused": ("compat", "StateCompatibilityRefused"),
    "StateDB": ("db", "StateDB"),
}


def __getattr__(name: str):
    if name in __all__:
        return lazy_import(name, _MODULES, __name__, globals())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
