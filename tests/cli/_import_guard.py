# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Import-layering helpers shared by the CLI tests.

The shared CLI modules (``_inspection``, ``_runs``, ``_engine_definitions``, ``_team_service``,
``_providers``, ``_orchestrate_parsing``) sit under the command modules, so none of them may
import a command module or the orchestrate package. These helpers read a module's source and
return every import with full dotted names, relative imports resolved against the module's
package, so ``from lionagi.cli.status import x`` and ``from .status import x`` are both visible.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: One-line imports of a command module or of the orchestrate package, as they can be written
#: from a module that lives directly in ``lionagi/cli``.
COMMAND_IMPORT_PROBES = (
    "from lionagi.cli.status import _x",
    "from .status import _x",
    "from . import status",
    "from ..cli.status import _x",
    "import lionagi.cli.status",
    "from lionagi.cli.orchestrate import _x",
    "from .orchestrate import _x",
    "from . import orchestrate",
)


def resolved_imports(source: str | Path, package: str) -> set[str]:
    """Every dotted name the module at ``source`` imports.

    Relative imports are resolved against ``package``, the package that holds the module.
    ``from a.b import c`` yields both ``a.b`` and ``a.b.c``, so a command module imported by
    name is visible as well as one imported by path.
    """
    parts = package.split(".")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(source).read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = parts[: len(parts) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def command_module_imports(imported: set[str]) -> set[str]:
    """The names in ``imported`` that sit under ``lionagi.cli.orchestrate`` or under a CLI
    command module (any public module or package directly under ``lionagi/cli``)."""
    import lionagi.cli

    cli_dir = Path(lionagi.cli.__file__).resolve().parent
    command_modules = {
        entry.stem
        for entry in cli_dir.iterdir()
        if not entry.name.startswith("_") and (entry.suffix == ".py" or entry.is_dir())
    }
    command_modules.add("orchestrate")
    return {
        name
        for name in imported
        if name.startswith("lionagi.cli.") and name.split(".")[2] in command_modules
    }


def source_with_line(source: str | Path, line: str, directory: Path) -> Path:
    """Write a copy of the module at ``source`` into ``directory`` with ``line`` appended, and
    return the copy's path."""
    target = directory / Path(source).name
    target.write_text(Path(source).read_text() + "\n" + line + "\n")
    return target
