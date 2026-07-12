# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import StateDB

__all__ = ("StateDB",)


def __getattr__(name: str):
    if name == "StateDB":
        from .db import StateDB

        globals()[name] = StateDB
        return StateDB
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
