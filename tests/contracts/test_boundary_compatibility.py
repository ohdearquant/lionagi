# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin declared package exports, installed extras, and settings names."""

import ast
import json
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests/contracts/data/boundary_compatibility.json"


def _getattr_alias_mappings(tree):
    """Resolve literal target/version forwarding maps looked up by a module getter."""
    mappings = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            value = None
        for target in targets:
            if isinstance(target, ast.Name):
                mappings.pop(target.id, None)
                if (
                    isinstance(value, dict)
                    and value
                    and all(
                        isinstance(name, str)
                        and isinstance(target, tuple)
                        and len(target) == 3
                        and all(isinstance(item, str) for item in target)
                        and "." in target[0]
                        and all(part.isidentifier() for part in target[0].split("."))
                        for name, target in value.items()
                    )
                ):
                    mappings[target.id] = value

    used = set()
    for getter in tree.body:
        if not isinstance(getter, ast.FunctionDef) or getter.name != "__getattr__":
            continue
        arguments = [*getter.args.posonlyargs, *getter.args.args]
        if not arguments:
            continue
        name_argument = arguments[0].arg
        nodes = []
        pending = list(getter.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            nodes.append(node)
            pending.extend(ast.iter_child_nodes(node))
        shadowed = (
            {
                node.id
                for node in nodes
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            }
            | {argument.arg for argument in arguments}
            | {
                node.asname or node.name.split(".")[0]
                for node in nodes
                if isinstance(node, ast.alias)
            }
        )
        shadowed.difference_update(
            name for node in nodes if isinstance(node, ast.Global) for name in node.names
        )
        for node in nodes:
            mapping = None
            key = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                mapping, key = node.func.value, node.args[0]
            elif isinstance(node, ast.Subscript):
                mapping, key = node.value, node.slice
            if (
                isinstance(mapping, ast.Name)
                and mapping.id in mappings
                and mapping.id not in shadowed
                and isinstance(key, ast.Name)
                and key.id == name_argument
            ):
                used.add(mapping.id)
    return {name: mappings[name] for name in used}


def _surface(root):
    """Extract declarations without importing the production modules."""
    exports = {}
    public_definitions = {}
    environment_lookups = {}
    moved_modules = None
    for path in sorted((root / "lionagi").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        alias_mappings = _getattr_alias_mappings(tree)
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
                used_mappings = [
                    target.id
                    for target in targets
                    if isinstance(target, ast.Name) and target.id in alias_mappings
                ]
                if used_mappings:
                    for name in used_mappings:
                        definitions.extend(alias_mappings[name])
                    entries.append(ast.get_source_segment(source, node))
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


@pytest.mark.parametrize("mapping_name", ["_OLD_NAMES", "FORWARDS"])
@pytest.mark.parametrize("lookup", ["{mapping}.get(name)", "{mapping}[name]"])
def test_surface_extraction_detects_lazy_alias_changes(tmp_path, mapping_name, lookup):
    package = tmp_path / "lionagi"
    package.mkdir()
    (package / "config.py").write_text("class AppSettings:\n    pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    module = package / "api.py"

    def write(aliases):
        module.write_text(
            f"{mapping_name} = {dict(sorted(aliases.items()))!r}\n"
            f"def __getattr__(name):\n    return {lookup.format(mapping=mapping_name)}\n",
            encoding="utf-8",
        )

    aliases = {
        "old": ("new.api.call", "0.37.0", "2.0.0"),
        "keep": ("new.api.keep", "0.37.0", "2.0.0"),
    }
    write(aliases)
    original = _surface(tmp_path)
    assert original["public_definitions"]["lionagi/api.py"] == ["keep", "old"]
    snapshot = json.loads(json.dumps(original))
    removed_alias = aliases.pop("old")
    write(aliases)
    changed = _surface(tmp_path)
    assert changed != snapshot
    assert set(snapshot["public_definitions"]["lionagi/api.py"]) - set(
        changed["public_definitions"]["lionagi/api.py"]
    ) == {"old"}
    aliases["old"] = removed_alias
    write(aliases)
    assert _surface(tmp_path) == snapshot
    aliases["extra"] = ("new.api.other", "0.37.0", "2.0.0")
    write(aliases)
    assert _surface(tmp_path)["public_definitions"] != original["public_definitions"]
    del aliases["extra"]
    aliases["old"] = ("new.api.changed", "0.37.0", "2.0.0")
    write(aliases)
    assert _surface(tmp_path)["package_exports"] != original["package_exports"]
    aliases["old"] = ("new.api.call", "0.38.0", "2.0.0")
    write(aliases)
    assert _surface(tmp_path)["package_exports"] != original["package_exports"]
    aliases.clear()
    write(aliases)
    assert "lionagi/api.py" not in _surface(tmp_path)["public_definitions"]


@pytest.mark.parametrize(
    "source",
    [
        "def __getattr__(name):\n    return MISSING.get(name)\n",
        "FORWARDS = dict(old=('new.api.call', '0.37.0', '2.0.0'))\n"
        "def __getattr__(name):\n    return FORWARDS.get(name)\n",
        "_DEPRECATED_ALIASES = {'old': ('new.api.call', '0.37.0', '2.0.0')}\n"
        "def __getattr__(name):\n    raise AttributeError(name)\n",
        "_DEPRECATED_ALIASES = {'old': ('new.api.call', '0.37.0', '2.0.0')}\n"
        "def __getattr__(name):\n    _DEPRECATED_ALIASES = {}\n"
        "    return _DEPRECATED_ALIASES.get(name)\n",
        "FIELDS = {'old': ('field', {})}\ndef __getattr__(name):\n    return FIELDS.get(name)\n",
        "FORWARDS = {'old': 'new.api.call'}\n"
        "def __getattr__(name):\n    return FORWARDS.get(name)\n",
        "FORWARDS = {'old': ('new.api.call', '0.37.0', '2.0.0')}\n"
        "def __getattr__(name):\n    from elsewhere import FORWARDS\n"
        "    return FORWARDS.get(name)\n",
    ],
    ids=["missing", "dynamic", "unused", "shadowed", "unrelated", "wrong-schema", "imported"],
)
def test_surface_extraction_does_not_invent_unresolved_lazy_exports(tmp_path, source):
    package = tmp_path / "lionagi"
    package.mkdir()
    (package / "config.py").write_text("class AppSettings:\n    pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (package / "api.py").write_text(source, encoding="utf-8")
    assert "lionagi/api.py" not in _surface(tmp_path)["public_definitions"]


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
