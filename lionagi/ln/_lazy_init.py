# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Thread-safe lazy initialization utility."""

import threading
from collections.abc import Callable

__all__ = ("LazyInit", "lazy_import")


class LazyInit:
    """Thread-safe one-shot initializer using double-checked locking."""

    __slots__ = ("_initialized", "_lock")

    def __init__(self) -> None:
        self._initialized = False
        self._lock = threading.RLock()

    @property
    def initialized(self) -> bool:
        """True after ensure() has completed."""
        return self._initialized

    def ensure(self, init_func: Callable[[], None]) -> None:
        """Execute init_func exactly once, thread-safely (double-checked locking)."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            init_func()
            self._initialized = True


def lazy_import(
    name: str,
    module_map: dict[str, tuple[str, str | None]],
    package: str,
    globs: dict,
) -> object:
    """Registry-based lazy import for module ``__getattr__``; raises AttributeError if name not in module_map."""
    if name not in module_map:
        raise AttributeError(f"module '{package}' has no attribute '{name}'")
    module_path, import_name = module_map[name]
    import importlib

    # Anchor relative imports on the parent package (package may be a module, not a package).
    pkg = globs.get("__package__") or package.rpartition(".")[0]
    mod = importlib.import_module(f".{module_path}", pkg)
    obj = getattr(mod, import_name or name)
    globs[name] = obj
    return obj
