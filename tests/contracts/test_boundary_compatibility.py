# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin declared package exports, installed extras, and settings names."""

import ast
import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests/contracts/data/boundary_compatibility.json"


def _surface(root):
    """Extract declarations without importing the production modules."""
    exports = {}
    public_definitions = {}
    environment_lookups = {}
    moved_modules = None
    for path in sorted((root / "lionagi").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        entries = []
        definitions = []
        for node in tree.body:
            if path.name == "__init__.py" and isinstance(node, (ast.Import, ast.ImportFrom)):
                entries.append(ast.get_source_segment(source, node))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Name)
                    and (target.id == "__all__" or "LAZY" in target.id.upper())
                    for target in targets
                ):
                    entries.append(ast.get_source_segment(source, node))
                if path.name == "_moved.py" and any(
                    isinstance(target, ast.Name) and target.id == "MOVED" for target in targets
                ):
                    moved_modules = ast.get_source_segment(source, node)
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__getattr__"
            ):
                entries.append(ast.get_source_segment(source, node))
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and not node.name.startswith("_"):
                definitions.append(node.name)
        if entries:
            exports[path.relative_to(root).as_posix()] = entries
        if definitions:
            public_definitions[path.relative_to(root).as_posix()] = sorted(definitions)
        lookups = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Call, ast.Subscript)):
                value = node.func if isinstance(node, ast.Call) else node.value
                spelling = ast.unparse(value)
                if spelling in {"os.getenv", "os.environ", "os.environ.get"}:
                    key = node.args[0] if isinstance(node, ast.Call) and node.args else None
                    if isinstance(node, ast.Subscript):
                        key = node.slice
                    if key is not None:
                        lookups.append(f"{spelling}:{ast.get_source_segment(source, key)}")
        if lookups:
            environment_lookups[path.relative_to(root).as_posix()] = sorted(lookups)

    source = (root / "lionagi/config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    settings = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AppSettings"
    )
    fields = [
        ast.get_source_segment(source, node)
        for node in settings.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    return {
        "package_exports": exports,
        "public_definitions": public_definitions,
        "environment_lookups": environment_lookups,
        "moved_modules": moved_modules,
        "settings_declarations": fields,
        "extras": project.get("optional-dependencies", {}),
        "console_scripts": project.get("scripts", {}),
    }


def test_declared_compatibility_surface_is_recorded():
    assert _surface(ROOT) == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_surface_extraction_detects_declaration_changes(tmp_path):
    package = tmp_path / "lionagi"
    package.mkdir()
    (package / "__init__.py").write_text('__all__ = ["original"]\n', encoding="utf-8")
    (package / "_moved.py").write_text('MOVED = [("old", "new")]\n', encoding="utf-8")
    (package / "api.py").write_text(
        'def call():\n    return os.getenv("EXAMPLE")\n', encoding="utf-8"
    )
    (package / "config.py").write_text('class AppSettings:\n    OLD: str = ""\n', encoding="utf-8")
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\n[project.scripts]\nli = "a:b"\n[project.optional-dependencies]\nx = []\n',
        encoding="utf-8",
    )
    original = _surface(tmp_path)
    (package / "__init__.py").write_text('__all__ = ["changed"]\n', encoding="utf-8")
    assert _surface(tmp_path)["package_exports"] != original["package_exports"]
    (package / "_moved.py").write_text("MOVED = []\n", encoding="utf-8")
    assert _surface(tmp_path)["moved_modules"] != original["moved_modules"]
    (package / "api.py").write_text(
        'def renamed():\n    return os.getenv("OTHER")\n', encoding="utf-8"
    )
    changed_api = _surface(tmp_path)
    assert changed_api["public_definitions"] != original["public_definitions"]
    assert changed_api["environment_lookups"] != original["environment_lookups"]
    (package / "config.py").write_text('class AppSettings:\n    NEW: str = ""\n', encoding="utf-8")
    assert _surface(tmp_path)["settings_declarations"] != original["settings_declarations"]
    project.write_text(
        '[project]\n[project.scripts]\nli = "c:d"\n[project.optional-dependencies]\ny = []\n',
        encoding="utf-8",
    )
    changed = _surface(tmp_path)
    assert changed["extras"] != original["extras"]
    assert changed["console_scripts"] != original["console_scripts"]
