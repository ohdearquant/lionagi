# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Import-layering helpers shared by the CLI tests.

The shared CLI modules (``_inspection``, ``_runs``, ``_engine_definitions``, ``_team_service``,
``_providers``, ``_orchestrate_parsing``) sit under the command modules, so none of them may
import a command module or the orchestrate package. These helpers read static imports and literal
dynamic imports from a module's source, resolving relative names against the module's package.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

_SHARED_DYNAMIC_COMMAND_IMPORT_PROBES = (
    "import importlib; importlib.import_module('lionhub.serve.cli.status')",
    "from importlib import import_module; import_module('lionhub.serve.cli.status')",
    "__import__('lionhub.serve.cli.orchestrate')",
    "from importlib import import_module as load; load('.status', 'lionhub.serve.cli')",
)

_DYNAMIC_COMMAND_IMPORT_PROBES = (
    *_SHARED_DYNAMIC_COMMAND_IMPORT_PROBES,
    "__import__('status', globals(), None, None, 1)",
    "__import__('', globals(), None, ['status'], 1)",
)

_ORCHESTRATION_DYNAMIC_COMMAND_IMPORT_PROBES = (
    *_SHARED_DYNAMIC_COMMAND_IMPORT_PROBES,
    "__import__('serve.cli.status', globals(), None, None, 2)",
    "__import__('serve.cli', globals(), None, ['status'], 2)",
)

#: One-line imports of a command module or of the orchestrate package, as they can be written
#: from a module that lives directly in ``lionhub/src/lionhub/serve/cli``.
_STATIC_COMMAND_IMPORT_PROBES = (
    "from lionhub.serve.cli.status import _x",
    "from .status import _x",
    "from . import status",
    "from ..cli.status import _x",
    "import lionhub.serve.cli.status",
    "from lionhub.serve.cli.orchestrate import _x",
    "from .orchestrate import _x",
    "from . import orchestrate",
)

COMMAND_IMPORT_PROBES = (*_STATIC_COMMAND_IMPORT_PROBES, *_DYNAMIC_COMMAND_IMPORT_PROBES)
TEAM_COMMAND_IMPORT_PROBES = (
    *_STATIC_COMMAND_IMPORT_PROBES,
    *_ORCHESTRATION_DYNAMIC_COMMAND_IMPORT_PROBES,
)


# Relative command names resolve under orchestration when used by these modules.
ORCHESTRATION_COMMAND_IMPORT_PROBES = (
    "from lionhub.serve.cli.status import _x",
    "from lionhub.serve.cli import status",
    "import lionhub.serve.cli.status",
    "from lionhub.serve.cli.orchestrate import _x",
    "from lionhub.serve.cli import orchestrate",
    *_ORCHESTRATION_DYNAMIC_COMMAND_IMPORT_PROBES,
)


def _call_argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
    if index < len(call.args):
        return call.args[index]
    return next((argument.value for argument in call.keywords if argument.arg == name), None)


def _builtin_imports(call: ast.Call, name: str, package: str) -> set[str]:
    level = _call_argument(call, 4, "level")
    if level is not None and (
        not isinstance(level, ast.Constant) or type(level.value) is not int or level.value < 0
    ):
        return set()
    depth = level.value if level is not None else 0
    if depth:
        if name.startswith(".") or not package or depth > len(package.split(".")):
            return set()
        name = resolve_name("." * depth + name, package)
    elif not name or name.startswith("."):
        # The builtin uses level for relativity, so a leading dot alone is not resolvable.
        return set()
    imported = {name}
    fromlist = _call_argument(call, 3, "fromlist")
    if isinstance(fromlist, ast.List | ast.Tuple) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in fromlist.elts
    ):
        imported.update(f"{name}.{item.value}" for item in fromlist.elts)
    return imported


def _dynamic_imports(nodes: list[ast.AST], package: str) -> set[str]:
    importlib_names: set[str] = set()
    import_module_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            importlib_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "importlib"
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "importlib":
            import_module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )

    imported: set[str] = set()
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        builtin = isinstance(function, ast.Name) and function.id == "__import__"
        by_name = isinstance(function, ast.Name) and function.id in import_module_names
        qualified = (
            isinstance(function, ast.Attribute)
            and function.attr == "import_module"
            and isinstance(function.value, ast.Name)
            and function.value.id in importlib_names
        )
        if not (builtin or by_name or qualified):
            continue
        name = _call_argument(node, 0, "name")
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            continue
        if builtin:
            imported.update(_builtin_imports(node, name.value, package))
        elif not name.value.startswith("."):
            if name.value:
                imported.add(name.value)
        else:
            anchor = _call_argument(node, 1, "package")
            if anchor is None or (isinstance(anchor, ast.Name) and anchor.id == "__package__"):
                package_name = package
            elif isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
                package_name = anchor.value
            else:
                continue
            try:
                imported.add(resolve_name(name.value, package_name))
            except ImportError:
                continue
    return imported


def resolved_imports(source: str | Path, package: str) -> set[str]:
    """Every dotted name the module at ``source`` imports.

    Relative imports are resolved against ``package``, the package that holds the module.
    ``from a.b import c`` yields both ``a.b`` and ``a.b.c``, so a command module imported by
    name is visible as well as one imported by path.
    Literal dynamic imports are also included. Relative ``import_module`` calls use a
    literal package or the source package. The builtin uses the source package and a
    literal level, and includes named entries of a literal fromlist. Computed arguments
    and names that cannot be resolved statically, including rebinding imported names, are
    outside this check.
    """
    parts = package.split(".")
    imported: set[str] = set()
    nodes = list(ast.walk(ast.parse(Path(source).read_text())))
    for node in nodes:
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
    return imported | _dynamic_imports(nodes, package)


def command_module_imports(imported: set[str]) -> set[str]:
    """The names in ``imported`` that sit under ``lionhub.serve.cli.orchestrate`` or under a CLI
    command module (any public module or package directly under ``lionhub/src/lionhub/serve/cli``)."""
    import lionhub.serve.cli

    cli_dir = Path(lionhub.serve.cli.__file__).resolve().parent
    command_modules = {
        entry.stem
        for entry in cli_dir.iterdir()
        if not entry.name.startswith("_") and (entry.suffix == ".py" or entry.is_dir())
    }
    command_modules.add("orchestrate")
    return {
        name
        for name in imported
        if name.startswith("lionhub.serve.cli.") and name.split(".")[3] in command_modules
    }


def source_with_line(source: str | Path, line: str, directory: Path) -> Path:
    """Write a copy of the module at ``source`` into ``directory`` with ``line`` appended, and
    return the copy's path."""
    target = directory / Path(source).name
    target.write_text(Path(source).read_text() + "\n" + line + "\n")
    return target
