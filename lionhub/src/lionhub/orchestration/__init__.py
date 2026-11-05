# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Multi-agent orchestration, loaded when its exported functions are requested."""

from __future__ import annotations

from typing import TYPE_CHECKING as _TYPE_CHECKING

if _TYPE_CHECKING:
    from .patterns import (
        build_dag_graph,
        build_fanout_graph,
        fanout,
        grant_spawn,
        normalize_dep_indices,
        plan,
        role_node_builder,
        spawn_roles,
    )

__all__ = (
    "spawn_roles",
    "plan",
    "fanout",
    "build_fanout_graph",
    "build_dag_graph",
    "role_node_builder",
    "grant_spawn",
    "normalize_dep_indices",
)

# Reloading the parent refreshes its re-exports from the canonical child.
for _export in __all__:
    globals().pop(_export, None)
del _export


def __getattr__(name: str) -> object:
    if name not in __all__ and name not in {"patterns", "prompts"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    if name == "prompts":
        module = import_module(".prompts", __name__)
        globals()[name] = module
        return module

    module = import_module(".patterns", __name__)
    # Take one snapshot, retaining any explicit replacements on the parent.
    exports = {export: getattr(module, export) for export in __all__}
    namespace = globals()
    for export, value in exports.items():
        namespace.setdefault(export, value)
    namespace["patterns"] = module
    return module if name == "patterns" else namespace[name]


def __dir__() -> list[str]:
    machinery = {"__getattr__", "__dir__", "_TYPE_CHECKING"}
    return sorted((set(globals()) - machinery) | set(__all__) | {"patterns", "prompts"})
