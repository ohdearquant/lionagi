# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi command-line interface — `li` entry point."""

from lionagi.ln import lazy_import

__all__ = ("main",)


def __getattr__(name: str):
    # Keep the composition root lazy when importing the CLI package itself.
    if name == "main":
        return lazy_import(name, {"main": ("main", "main")}, __name__, globals())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
