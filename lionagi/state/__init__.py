# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import SchemaTooNewError, StateDB

__all__ = ("SchemaTooNewError", "StateDB")


def __getattr__(name: str):
    if name in __all__:
        from . import db

        value = getattr(db, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
