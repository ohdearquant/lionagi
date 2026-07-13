# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi command-line interface — `li` entry point."""

__all__ = ("main",)


def __getattr__(name: str):
    # Lazy: importing lionagi.studio.cli (which main.py imports at module
    # level) must not re-enter main.py through this package init.
    if name == "main":
        from .main import main

        # Pin the function over the submodule main.py binds here, so
        # `from lionagi.cli import main` yields the callable, not the module.
        globals()["main"] = main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
