# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: names that must stay importable at lionagi.cli.orchestrate module level.

Covers the validate_path_component re-export and the parser builders, which are defined in
lionagi.cli._orchestrate_parsing and re-exported here.
"""

from __future__ import annotations


def test_validate_path_component_importable_from_orchestrate_package() -> None:
    """``from lionagi.cli.orchestrate import validate_path_component`` must succeed."""
    from lionagi.cli.orchestrate import validate_path_component  # noqa: F401 — import is the test

    assert callable(validate_path_component), "validate_path_component must be a callable function"


def test_validate_path_component_is_the_canonical_function() -> None:
    """The re-export must resolve to the same object as the canonical location."""
    from lionagi.cli.orchestrate import validate_path_component as from_orchestrate
    from lionagi.libs.path_safety import validate_path_component as canonical

    assert from_orchestrate is canonical, (
        "lionagi.cli.orchestrate.validate_path_component must be the same object "
        "as lionagi.libs.path_safety.validate_path_component"
    )


def _resolved_imports(module_file: str, package: str) -> set[str]:
    """Every dotted name the module at ``module_file`` imports, relative imports resolved
    against ``package`` (the package that holds the module)."""
    import ast
    from pathlib import Path

    parts = package.split(".")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(module_file).read_text())):
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


def _orchestrate_or_command_imports(imported: set[str]) -> set[str]:
    """The names in ``imported`` that sit under ``lionagi.cli.orchestrate`` or under a CLI
    command module."""
    from pathlib import Path

    from lionagi.cli import orchestrate

    cli_dir = Path(orchestrate.__file__).resolve().parent.parent
    command_modules = {
        entry.stem
        for entry in cli_dir.iterdir()
        if not entry.name.startswith("_") and (entry.suffix == ".py" or entry.is_dir())
    }
    package = "lionagi.cli.orchestrate"
    return {
        name
        for name in imported
        if name == package
        or name.startswith(package + ".")
        or (name.startswith("lionagi.cli.") and name.split(".")[2] in command_modules)
    }


def test_orchestrate_parser_construction_lives_in_the_parsing_module() -> None:
    """The parser builders are defined in ``_orchestrate_parsing``, the package re-exports the
    same objects, and neither that module nor the ``_providers`` module it builds on imports
    the orchestrate package, a sibling module, or a CLI command module."""
    import importlib

    from lionagi.cli import orchestrate

    parsing = importlib.import_module("lionagi.cli._orchestrate_parsing")
    providers = importlib.import_module("lionagi.cli._providers")

    assert parsing.add_orchestrate_subparser.__module__ == "lionagi.cli._orchestrate_parsing"
    assert parsing._add_mcp_config_args.__module__ == "lionagi.cli._orchestrate_parsing"
    assert orchestrate.add_orchestrate_subparser is parsing.add_orchestrate_subparser
    assert orchestrate._add_mcp_config_args is parsing._add_mcp_config_args

    parsing_imports = _resolved_imports(parsing.__file__, "lionagi.cli")
    # The walk resolved the relative import, so the check below is not vacuous.
    assert "lionagi.cli._providers" in parsing_imports
    assert not _orchestrate_or_command_imports(parsing_imports)

    providers_imports = _resolved_imports(providers.__file__, "lionagi.cli")
    assert providers_imports, "expected the module to import something"
    assert not _orchestrate_or_command_imports(providers_imports)
