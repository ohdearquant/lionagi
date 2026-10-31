# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Old import paths of modules that moved out of ``lionagi``, kept until 2.0.

Each row of :data:`MOVED` maps an old dotted prefix to the prefix the code lives under
now. Importing an old name returns the module object already registered under the new
name, so both names share one module: classes, functions and module globals are the
same objects, ``isinstance`` and ``unittest.mock.patch`` on the old path keep working,
and pickles that name the old path still load. Each old module emits one
``DeprecationWarning`` naming its new path and the release that removes the old one.

There are no forwarding files at the old locations, so listing ``lionagi.__path__`` with
``pkgutil`` does not show a moved package; list the new package instead. Removing a move
is deleting its row and updating the docs that still name its old path; its CHANGELOG
lines stay, as the history of the release that made the move. lionagi 2.0 removes every
row, this module and the tests that enumerate the table.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import warnings
from types import CodeType, ModuleType
from typing import NamedTuple

__all__ = ("MOVED", "Moved", "install", "resolve", "still_on_disk")

_LIONAGI_DIR = os.path.dirname(os.path.abspath(__file__))


class Moved(NamedTuple):
    """One moved package or module: ``old`` and everything below it now lives under ``new``."""

    old: str
    new: str
    deprecated_in: str
    removed_in: str


MOVED: tuple[Moved, ...] = (
    Moved("lionagi.orchestration", "lionhub.orchestration", "0.37.0", "2.0.0"),
    Moved("lionagi.engines", "lionhub.orchestration.engines", "0.37.0", "2.0.0"),
    Moved("lionagi.dispatch", "lionhub.orchestration.dispatch", "0.37.0", "2.0.0"),
    Moved("lionagi.work", "lionhub.orchestration.work", "0.37.0", "2.0.0"),
    Moved("lionagi.mcp", "lionhub.serve.mcp", "0.37.0", "2.0.0"),
    Moved("lionagi.providers", "lionhub.providers", "0.37.0", "2.0.0"),
    Moved("lionagi.providers._provider_errors", "lionagi._provider_errors", "0.37.0", "2.0.0"),
    Moved(
        "lionagi.providers._secret_resolution",
        "lionagi.service.accounts._secret_resolution",
        "0.37.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.providers._subagent_admission",
        "lionagi.service._subagent_admission",
        "0.37.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.providers.openai._chat_schemas",
        "lionagi.service.connections._chat_schemas",
        "0.37.0",
        "2.0.0",
    ),
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

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        # The import system makes a placeholder, and exec_module replaces it in
        # ``sys.modules`` with the moved module, which the import system then returns.
        # Nothing is written onto the moved module itself, its spec included, so
        # imports through two old names of one module cannot disturb each other.
        return None

    def exec_module(self, module: ModuleType) -> None:
        moved = _import_new(self._new_name)
        warnings.warn(
            f"{module.__name__} moved to {self._new_name}; the old import path is removed "
            f"in lionagi {self._row.removed_in}. Import from {self._new_name} instead.",
            DeprecationWarning,
            stacklevel=_caller_stacklevel(),
        )
        sys.modules[module.__name__] = moved

    def get_data(self, path: str) -> bytes:
        # Resource readers such as ``pkgutil.get_data`` ask the old name's loader.
        return importlib.import_module(self._new_name).__spec__.loader.get_data(path)

    def get_code(self, fullname: str) -> CodeType | None:
        # ``python -m <old name>`` runs a module's code instead of importing it, so runpy
        # asks the old name's loader for the code of the module at the new name.
        return importlib.util.find_spec(self._new_name).loader.get_code(self._new_name)


class _MovedFinder(importlib.abc.MetaPathFinder):
    lionagi_moved = True

    def find_spec(self, fullname, path=None, target=None):
        row = _row(fullname)
        if row is None or still_on_disk(fullname):
            return None
        new_name = row.new + fullname[len(row.old) :]
        new_spec = importlib.util.find_spec(new_name)
        if new_spec is None:
            return None
        # Describe the old name the way the new module is described, so a lookup that
        # has not imported it yet still sees a package where the new module is one.
        spec = importlib.machinery.ModuleSpec(
            fullname,
            _AliasLoader(row, new_name),
            origin=new_spec.origin,
            is_package=new_spec.submodule_search_locations is not None,
        )
        if new_spec.submodule_search_locations is not None:
            spec.submodule_search_locations = list(new_spec.submodule_search_locations)
        return spec


# One retry is the usual case: once this side has given way, its next attempt waits for the
# package instead of failing again. The rest is headroom for a thread that is slow to wake.
_DEADLOCK_RETRIES = 20
# importlib's deadlock error is private. On a Python without it nothing is retried, and an
# import error still surfaces as itself, not as a failure to evaluate the except clause.
_DEADLOCK_ERROR = getattr(importlib._bootstrap, "_DeadlockError", ())


def _import_new(new_name: str) -> ModuleType:
    import time

    for attempt in range(1, _DEADLOCK_RETRIES + 1):
        try:
            return importlib.import_module(new_name)
        except _DEADLOCK_ERROR:
            # An old name reaches the new package through its top, so the package's __init__
            # can ask for a submodule lock that a thread importing the new name already holds
            # while it waits for the package. importlib breaks that cycle by failing this side,
            # which releases the package. Importing again at once would take it back before the
            # waiting thread wakes, so this side first gives way; the other thread then finishes
            # the package and the next attempt returns it.
            if attempt == _DEADLOCK_RETRIES:
                raise
            time.sleep(0.001 * attempt)
    raise AssertionError("unreachable")


def install() -> None:
    """Put the finder for moved modules first on ``sys.meta_path``, once."""
    if any(getattr(finder, "lionagi_moved", False) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _MovedFinder())
