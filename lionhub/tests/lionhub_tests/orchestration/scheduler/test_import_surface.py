# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: names that must stay importable at lionagi.cli.orchestrate module level.

Covers the validate_path_component re-export and the parser builders, which are defined in
lionhub.orchestration.flow.parsing and re-exported here.
"""

from __future__ import annotations


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


def test_scheduler_reserved_flags_come_from_the_canonical_flow_parser(monkeypatch):
    """A parser option added at the shared builder is reserved without a second flag table."""
    from lionhub.orchestration.flow import parsing
    from lionhub.orchestration.scheduler import subprocess as scheduler_subprocess

    original = parsing.add_orchestrate_subparser
    expected = []

    def record(subparsers):
        parsers = original(subparsers)
        flow = parsers["flow"]
        flow.add_argument("--example-scheduler-option")
        expected.append(
            frozenset(
                option
                for action in flow._actions
                for option in action.option_strings
                if option.startswith("--")
            )
        )
        return parsers

    monkeypatch.setattr(parsing, "add_orchestrate_subparser", record)
    import lionhub.orchestration.schedule.action_validation as action_validation

    monkeypatch.setattr(action_validation, "_RESERVED_FLOW_FLAGS", None)
    actual = action_validation._reserved_flow_flags()
    assert len(expected) == 1
    assert actual == expected[0]
    assert "--example-scheduler-option" in actual
    assert action_validation._reserved_flow_flags() is actual
    assert len(expected) == 1
