# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi command-line interface — `li` entry point."""

__all__ = ("main",)


def __getattr__(name: str):
    # Lazy so that importing any lionagi.cli submodule (e.g. from
    # lionagi.studio.cli, which main.py itself imports at module level)
    # never re-enters main.py through the package init.
    if name == "main":
        from .main import main

        # Importing .main also binds the submodule as this package's `main`
        # attribute; pin the function over it so `from lionagi.cli import
        # main` yields the callable, not the module.
        globals()["main"] = main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
