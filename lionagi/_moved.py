# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Old import paths of modules that moved out of ``lionagi``, kept until 2.0.

Each row of :data:`MOVED` maps an old dotted prefix to the prefix the code lives under
now. Importing an old name returns the module object already registered under the new
name, so both names share one module: classes, functions and module globals are the
same objects, ``isinstance`` and ``unittest.mock.patch`` on the old path keep working,
and pickles that name the old path still load. Each old module emits one
``DeprecationWarning`` naming its new path and the release that removes the old one.

There are no forwarding files at the old locations. Removing a move is deleting its row.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import os
import sys
import warnings
from types import ModuleType
from typing import NamedTuple

__all__ = ("MOVED", "Moved", "install", "resolve", "still_on_disk")

_LIONAGI_DIR = os.path.dirname(os.path.abspath(__file__))


class Moved(NamedTuple):
    """One moved package: ``old`` and everything below it now lives under ``new``."""

    old: str
    new: str
    deprecated_in: str
    removed_in: str


MOVED: tuple[Moved, ...] = (
    Moved("lionagi.orchestration", "lionhub.orchestration", "0.37.0", "2.0.0"),
)


def _row(fullname: str) -> Moved | None:
    best: Moved | None = None
    for row in MOVED:
        if fullname == row.old or fullname.startswith(row.old + "."):
            if best is None or len(row.old) > len(best.old):
                best = row
    return best


def resolve(fullname: str) -> str | None:
    """Return the current dotted name for ``fullname``, or None if it did not move."""
    row = _row(fullname)
    if row is None:
        return None
    return row.new + fullname[len(row.old) :]


def still_on_disk(fullname: str) -> bool:
    """Whether a module file for ``fullname`` still exists inside the lionagi package.

    The finder never serves such a name, so a package that was copied rather than
    moved imports as its old self and fails the moved-modules tests.
    """
    parts = fullname.split(".")[1:]
    if not parts:
        return False
    base = os.path.join(_LIONAGI_DIR, *parts)
    return os.path.isfile(base + ".py") or os.path.isfile(os.path.join(base, "__init__.py"))


def _is_import_machinery(filename: str) -> bool:
    return filename == __file__ or filename == importlib.__file__


def _caller_stacklevel() -> int:
    # Point the warning at the code that wrote the import. ``warnings`` already skips
    # the frozen importlib frames without counting them, so count only the frames it
    # counts: this module's own and ``importlib.import_module``.
    frame = sys._getframe(1)
    level = 1
    while frame.f_back is not None:
        frame = frame.f_back
        filename = frame.f_code.co_filename
        if "importlib" in filename and "_bootstrap" in filename:
            continue
        level += 1
        if not _is_import_machinery(filename):
            break
    return level


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, row: Moved, new_name: str) -> None:
        self._row = row
        self._new_name = new_name
        self._spec: importlib.machinery.ModuleSpec | None = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self._new_name)
        self._spec = module.__spec__
        warnings.warn(
            f"{spec.name} moved to {self._new_name}; the old import path is removed in "
            f"lionagi {self._row.removed_in}. Import from {self._new_name} instead.",
            DeprecationWarning,
            stacklevel=_caller_stacklevel(),
        )
        return module

    def exec_module(self, module: ModuleType) -> None:
        # The import system stamped the alias spec onto the shared module object.
        # Put the module's own spec back, so ``importlib.reload`` and relative imports
        # inside it still resolve against its real name.
        module.__spec__ = self._spec


class _MovedFinder(importlib.abc.MetaPathFinder):
    lionagi_moved = True

    def find_spec(self, fullname, path=None, target=None):
        row = _row(fullname)
        if row is None or still_on_disk(fullname):
            return None
        new_name = row.new + fullname[len(row.old) :]
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(row, new_name))


def install() -> None:
    """Put the finder for moved modules first on ``sys.meta_path``, once."""
    if any(getattr(finder, "lionagi_moved", False) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _MovedFinder())
