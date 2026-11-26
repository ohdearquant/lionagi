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
from collections.abc import Iterable, Mapping
from types import CodeType, MappingProxyType, ModuleType
from typing import NamedTuple

__all__ = ("MOVED", "Moved", "install", "resolve", "resolve_legacy", "still_on_disk")

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
    Moved("lionagi.lndl", "lionhub.lndl", "0.37.0", "2.0.0"),
    Moved("lionagi.tools", "lionhub.tools", "0.38.0", "2.0.0"),
    Moved("lionagi.agent", "lionhub.agent", "0.38.0", "2.0.0"),
    Moved("lionagi.casts", "lionhub.agent.casts", "0.38.0", "2.0.0"),
    Moved("lionagi.hooks.external", "lionhub.hooks.external", "0.38.0", "2.0.0"),
    Moved("lionagi.operations.lndl_middle", "lionhub.lndl.middle", "0.37.0", "2.0.0"),
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
    Moved("lionagi.cli", "lionhub.serve.cli", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._argtypes", "lionhub.orchestration.arguments", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._code_identity", "lionhub.serve.diagnostics", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._logging", "lionhub.orchestration.console", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._mirror_settings", "lionhub.serve.mirror_settings", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._project", "lionhub.orchestration.project", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._mcp_resolve", "lionhub.orchestration.mcp_config", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._team_service", "lionhub.orchestration.team", "0.38.0", "2.0.0"),
    Moved(
        "lionagi.cli._engine_definitions",
        "lionhub.orchestration.engine_definitions",
        "0.38.0",
        "2.0.0",
    ),
    Moved("lionagi.cli._util", "lionhub.orchestration.job", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._inspection", "lionhub.orchestration.inspection", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._providers", "lionhub.orchestration.spawn", "0.38.0", "2.0.0"),
    Moved(
        "lionagi.cli._orchestrate_parsing", "lionhub.orchestration.flow.parsing", "0.38.0", "2.0.0"
    ),
    Moved(
        "lionagi.cli._schedule_parsing", "lionhub.orchestration.schedule.parsing", "0.38.0", "2.0.0"
    ),
    Moved("lionagi.cli._mirror_service", "lionhub.serve.mirror", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._runs", "lionhub.orchestration.runs", "0.38.0", "2.0.0"),
    Moved("lionagi.cli._kill_service", "lionhub.orchestration.kill", "0.38.0", "2.0.0"),
    Moved(
        "lionagi.cli.orchestrate._checkpoint",
        "lionhub.orchestration.flow.checkpoint",
        "0.38.0",
        "2.0.0",
    ),
    Moved("lionagi._flow_spec", "lionhub.orchestration.flow.spec", "0.39.0", "2.0.0"),
    Moved(
        "lionagi._checkpoint_version",
        "lionhub.orchestration.flow.checkpoint_version",
        "0.39.0",
        "2.0.0",
    ),
    Moved(
        "lionagi._checkpoint_v3_codec",
        "lionhub.orchestration.flow.checkpoint_codec",
        "0.39.0",
        "2.0.0",
    ),
    Moved("lionagi._command_env", "lionhub.tools.command_env", "0.39.0", "2.0.0"),
    Moved("lionagi._command_env_metadata", "lionhub.tools.command_env_metadata", "0.39.0", "2.0.0"),
    Moved("lionagi._spec_limits", "lionhub.orchestration.spec_limits", "0.39.0", "2.0.0"),
    Moved("lionagi.ln._paths", "lionhub.tools._paths", "0.39.0", "2.0.0"),
    # The Studio application. Daemon-owned modules that once lived under lionagi.studio keep
    # their own, longer rows, which win over this one. lionagi.studio and
    # lionagi.studio.services stay in core as empty packages, and lionagi.studio.operator as
    # one that only forwards three names, so those modules' old names import without the
    # application installed; the finder steps aside for all three.
    Moved("lionagi.studio", "lionagi_studio", "0.39.0", "2.0.0"),
    Moved(
        "lionagi.studio.services._schedule_validation",
        "lionhub.orchestration.schedule.validation",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.schedule_action_validation",
        "lionhub.orchestration.schedule.action_validation",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.schedule_timing",
        "lionhub.orchestration.schedule.timing",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio._timezone_rules",
        "lionhub.orchestration.schedule._timezone_rules",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.schedule_execution_root",
        "lionhub.orchestration.schedule.execution_root",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.schedule_declaration",
        "lionhub.orchestration.schedule.declaration",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.schedule_export",
        "lionhub.orchestration.schedule.export",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.project_lookup",
        "lionhub.orchestration.project_store",
        "0.38.0",
        "2.0.0",
    ),
    Moved("lionagi.studio.scheduler", "lionhub.orchestration.scheduler", "0.38.0", "2.0.0"),
    Moved(
        "lionagi.studio.services.scheduler_state",
        "lionhub.orchestration.scheduler.scheduler_state",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.db_maintenance",
        "lionhub.orchestration.scheduler.db_maintenance",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.retention_archive",
        "lionhub.orchestration.scheduler.retention_archive",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.lifecycle",
        "lionhub.orchestration.scheduler.lifecycle",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.terminal_reconciliation",
        "lionhub.orchestration.scheduler.terminal_reconciliation",
        "0.38.0",
        "2.0.0",
    ),
    Moved("lionagi.studio.services._db", "lionhub.orchestration.state_store", "0.38.0", "2.0.0"),
    Moved("lionagi.integrations", "lionhub.integrations", "0.38.0", "2.0.0"),
    Moved(
        "lionagi.studio._host",
        "lionhub.serve.daemon._host",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio._traceback_dump",
        "lionhub.serve.daemon._traceback_dump",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.daemon_log",
        "lionhub.serve.daemon.daemon_log",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.reconnect",
        "lionhub.serve.daemon.reconnect",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services._io",
        "lionhub.serve.daemon.services._io",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services._path_safety",
        "lionhub.serve.daemon.services._path_safety",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services._sse",
        "lionhub.serve.daemon.services._sse",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.artifact_verification",
        "lionhub.serve.daemon.services.artifact_verification",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.signals",
        "lionhub.serve.daemon.services.signals",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.run_view",
        "lionhub.serve.daemon.services.run_view",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.task_applications",
        "lionhub.serve.daemon.services.task_applications",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.operator.redact", "lionhub.serve.daemon.services.redact", "0.38.0", "2.0.0"
    ),
    Moved("lionagi.studio.launch_access", "lionhub.serve.daemon.launch_access", "0.38.0", "2.0.0"),
    Moved("lionagi.studio.services.runs", "lionhub.serve.daemon.services.runs", "0.38.0", "2.0.0"),
    Moved(
        "lionagi.studio.services.run_resume",
        "lionhub.serve.daemon.services.run_resume",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.run_resume_worker",
        "lionhub.serve.daemon.services.run_resume_worker",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.engine_runs",
        "lionhub.serve.daemon.services.engine_runs",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.invocations",
        "lionhub.serve.daemon.services.invocations",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.launches",
        "lionhub.serve.daemon.services.launches",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.identity",
        "lionhub.serve.daemon.services.identity",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.pairing",
        "lionhub.serve.daemon.services.pairing",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.sessions",
        "lionhub.serve.daemon.services.sessions",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services.admin", "lionhub.serve.daemon.services.admin", "0.38.0", "2.0.0"
    ),
    Moved(
        "lionagi.studio.services.schedules",
        "lionhub.serve.daemon.services.schedules",
        "0.38.0",
        "2.0.0",
    ),
    Moved(
        "lionagi.studio.services._session_tail",
        "lionhub.serve.daemon.services._session_tail",
        "0.38.0",
        "2.0.0",
    ),
)


def _index_by_first_component(rows: Iterable[Moved]) -> Mapping[str, tuple[Moved, ...]]:
    """Group ``rows`` by the first dotted component of ``old``, keeping their order."""
    buckets: dict[str, list[Moved]] = {}
    for row in rows:
        buckets.setdefault(row.old.split(".", 1)[0], []).append(row)
    return MappingProxyType({first: tuple(bucket) for first, bucket in buckets.items()})


# The finder asks about every module name Python looks up. A lookup checks only the rows whose
# ``old`` begins with the same first component as the name, taken from an index of ``MOVED``
# that follows its current value: it is rebuilt when ``MOVED`` is rebound to another tuple.
# Every current ``old`` begins with ``lionagi``, so a lionagi name still checks every row.
_INDEX: tuple[tuple[Moved, ...], Mapping[str, tuple[Moved, ...]]] = (
    MOVED,
    _index_by_first_component(MOVED),
)


def _row(fullname: str) -> Moved | None:
    global _INDEX
    table = MOVED
    pair = _INDEX
    if pair[0] is not table:
        # The table and its index are one tuple replaced in one statement, so a thread can
        # rebuild redundantly but never pairs one table with another table's index.
        pair = _INDEX = (table, _index_by_first_component(table))
    best: Moved | None = None
    for row in pair[1].get(fullname.split(".", 1)[0], ()):
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


def _index_by_new_first_component(
    table: tuple[Moved, ...],
) -> Mapping[str, tuple[Moved, ...]]:
    return MappingProxyType(
        {
            first: tuple(row for row in table if row.new.split(".", 1)[0] == first)
            for first in {row.new.split(".", 1)[0] for row in table}
        }
    )


_REVERSE_INDEX: tuple[tuple[Moved, ...], Mapping[str, tuple[Moved, ...]]] = (
    MOVED,
    _index_by_new_first_component(MOVED),
)


def resolve_legacy(fullname: str) -> str | None:
    """Return the legacy dotted name for a current name covered by a move, or None."""
    global _REVERSE_INDEX
    table = MOVED
    pair = _REVERSE_INDEX
    if pair[0] is not table:
        pair = _REVERSE_INDEX = (table, _index_by_new_first_component(table))
    best: Moved | None = None
    for row in pair[1].get(fullname.split(".", 1)[0], ()):
        if fullname == row.new or fullname.startswith(row.new + "."):
            if best is None or len(row.new) > len(best.new):
                best = row
    if best is None:
        return None
    return best.old + fullname[len(best.new) :]


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


# A root lazy export (``lionagi.LNDLOutput``) reaches a moved module through the root
# ``__getattr__`` and the lazy-import helper, so both stand between the caller and this loader.
_LAZY_EXPORT_FRAMES = frozenset(
    {
        (os.path.join(os.path.dirname(__file__), "__init__.py"), "__getattr__"),
        (os.path.join(os.path.dirname(__file__), "ln", "_lazy_init.py"), "lazy_import"),
    }
)


def _is_import_machinery(code: CodeType) -> bool:
    filename = code.co_filename
    return (
        filename == __file__
        or filename == importlib.__file__
        or (filename, code.co_name) in _LAZY_EXPORT_FRAMES
    )


def _warning_origin() -> tuple[int, bool]:
    # Point the warning at the code that wrote the import. ``warnings`` already skips
    # the frozen importlib frames without counting them, so count only the frames it
    # counts: this module's own, ``importlib.import_module``, and a root lazy export's two.
    frame = sys._getframe(1)
    level = 1
    while frame.f_back is not None:
        frame = frame.f_back
        filename = frame.f_code.co_filename
        if "importlib" in filename and "_bootstrap" in filename:
            continue
        level += 1
        if not _is_import_machinery(frame.f_code):
            command_line = (
                frame.f_globals.get("__name__") == "runpy"
                and frame.f_code.co_name == "_get_module_details"
                and sys.argv[:1] == ["-m"]
            )
            return level, command_line
    return level, False


def _has_aliased_parent(fullname: str) -> bool:
    parent_name = fullname.rpartition(".")[0]
    parent = sys.modules.get(parent_name)
    canonical_parent = resolve(parent_name)
    return (
        parent is not None
        and canonical_parent is not None
        and parent is sys.modules.get(canonical_parent)
    )


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
        self._warn_moved(module.__name__)
        sys.modules[module.__name__] = moved

    def _warn_moved(self, old_name: str, *, loading_code: bool = False) -> None:
        stacklevel, command_line = _warning_origin()
        if command_line and not loading_code:
            # runpy imports the parents first. Warn for the entry when it gets code.
            return
        if loading_code and not command_line and _has_aliased_parent(old_name):
            # An aliased parent already warned during a programmatic runpy import.
            return
        message = (
            f"{old_name} moved to {self._new_name}; the old import path is removed "
            f"in lionagi {self._row.removed_in}. Import from {self._new_name} instead."
        )
        if command_line:
            warnings.warn_explicit(
                message, DeprecationWarning, "<command line>", 1, module="__main__"
            )
        else:
            warnings.warn(message, DeprecationWarning, stacklevel=stacklevel)

    def get_data(self, path: str) -> bytes:
        # Resource readers such as ``pkgutil.get_data`` ask the old name's loader.
        return importlib.import_module(self._new_name).__spec__.loader.get_data(path)

    def get_code(self, fullname: str) -> CodeType | None:
        code = importlib.util.find_spec(self._new_name).loader.get_code(self._new_name)
        if code is not None:
            self._warn_moved(fullname, loading_code=True)
        return code


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
