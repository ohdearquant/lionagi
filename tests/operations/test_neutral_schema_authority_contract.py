# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Architecture gates for neutral schema declaration authority."""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "lionagi"
_CREATE_MODEL_COMPAT_SHIMS = {
    Path("adapters/spec_adapters/_protocol.py"),
    Path("ln/types/operable.py"),
}
_FIELD_MODEL_COMPAT_MODULES = {
    Path("__init__.py"),
    Path("models/__init__.py"),
    Path("models/_build_model.py"),
    Path("models/operable_model.py"),
    Path("operations/ReAct/ReAct.py"),
    Path("operations/fields.py"),
    Path("operations/operate/operate.py"),
    Path("operations/operate/step.py"),
    Path("session/branch.py"),
}
_FIELD_MODEL_CONSTRUCTION_FACADES = {Path("operations/fields.py")}


def _production_trees():
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT)
        yield relative, ast.parse(path.read_text(), filename=str(relative))


def _position(relative: Path, node: ast.AST) -> str:
    return f"{relative}:{node.lineno}"


def test_production_does_not_construct_legacy_operable_models():
    violations: list[str] = []

    for relative, tree in _production_trees():
        aliases = {"OperableModel"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "OperableModel"
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in aliases:
                violations.append(_position(relative, node))
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "OperableModel":
                violations.append(_position(relative, node))

    assert not violations, (
        "Production must declare schemas with Spec/Operable, not construct "
        f"OperableModel: {violations}"
    )


def test_production_materializes_operables_through_explicit_adapters():
    violations: list[str] = []

    for relative, tree in _production_trees():
        if relative in _CREATE_MODEL_COMPAT_SHIMS:
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_model"
            ):
                continue

            receiver = ast.unparse(node.func.value)
            if "adapter" not in receiver.lower():
                violations.append(f"{_position(relative, node)} ({receiver}.create_model)")

    assert not violations, (
        "Internal callers must materialize through an explicit schema adapter; "
        "Operable.create_model remains only as a public compatibility shim: "
        f"{violations}"
    )


def _field_model_inventory(trees):
    imports: set[Path] = set()
    constructions: set[Path] = set()

    for relative, tree in trees:
        aliases = {"FieldModel"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "FieldModel":
                        imports.add(relative)
                        aliases.add(alias.asname or alias.name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in aliases
            ):
                constructions.add(relative)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "FieldModel"
            ):
                constructions.add(relative)

    return imports, constructions


def test_field_model_references_match_the_compatibility_inventory():
    imports, constructions = _field_model_inventory(_production_trees())

    assert imports == _FIELD_MODEL_COMPAT_MODULES
    assert constructions == _FIELD_MODEL_CONSTRUCTION_FACADES


def test_field_model_inventory_detects_import_alias_construction():
    source = "from lionagi.models import FieldModel as FM\nFM(int)\n"

    imports, constructions = _field_model_inventory(
        ((Path("synthetic_alias.py"), ast.parse(source)),)
    )

    assert imports == {Path("synthetic_alias.py")}
    assert constructions == {Path("synthetic_alias.py")}


def test_field_model_inventory_detects_qualified_construction():
    source = "import lionagi.models as models\nmodels.FieldModel(int)\n"

    _, constructions = _field_model_inventory(
        ((Path("synthetic_qualified.py"), ast.parse(source)),)
    )

    assert constructions == {Path("synthetic_qualified.py")}
