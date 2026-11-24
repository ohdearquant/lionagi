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
# The record is split into pieces so that no file grows toward the data-file size limit as
# exports are added: the sections keyed by module path go into one piece per package and
# first-level subpackage (`lionhub.providers.json`, with a package's own top-level modules in
# `lionhub.json`), and the sections about the project as a whole go into `project.json`.
# Regenerate with `python tests/contracts/test_boundary_compatibility.py`.
SNAPSHOT_DIR = ROOT / "tests/contracts/data/boundary_compatibility"
_PATH_SECTIONS = ("package_exports", "public_definitions", "environment_lookups")
_PROJECT_SECTIONS = ("moved_modules", "settings_declarations", "extras", "console_scripts")
_PROJECT_PIECE = "project"
_PACKAGE_ROOTS = (
    ("lionagi/", "lionagi"),
    ("lionhub/src/lionhub/", "lionhub"),
    ("apps/studio/backend/src/lionagi_studio/", "lionagi_studio"),
)


def _getattr_alias_mappings(tree):
    """Resolve literal target/removal forwarding maps looked up by a module getter."""
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
                        and len(target) == 2
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
    # The hub and Studio application packages carry modules moved out of lionagi; their
    # exports, definitions and environment reads are part of the same compatibility surface.
    moved_roots = (root / "lionhub/src/lionhub", root / "apps/studio/backend/src/lionagi_studio")
    paths = [
        *(root / "lionagi").rglob("*.py"),
        *(path for base in moved_roots if base.is_dir() for path in base.rglob("*.py")),
    ]
    for path in sorted(paths):
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


def _piece_name(path):
    """Name the piece that records *path*: its package, and its first subpackage if any."""
    for prefix, package in _PACKAGE_ROOTS:
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            return f"{package}.{rest.split('/')[0]}" if "/" in rest else package
    raise ValueError(f"{path} is under no package root the snapshot knows")


def _split(surface):
    """Divide *surface* into the pieces the snapshot directory holds, keyed by piece name."""
    if set(surface) != {*_PATH_SECTIONS, *_PROJECT_SECTIONS}:
        raise ValueError(f"the surface has sections {sorted(surface)}")
    pieces = {_PROJECT_PIECE: {section: surface[section] for section in _PROJECT_SECTIONS}}
    for section in _PATH_SECTIONS:
        for path, value in surface[section].items():
            pieces.setdefault(_piece_name(path), {}).setdefault(section, {})[path] = value
    return pieces


def _join(pieces):
    """Assemble *pieces* into the one surface mapping; a piece out of shape fails."""
    if _PROJECT_PIECE not in pieces:
        raise ValueError(f"the snapshot has no {_PROJECT_PIECE} piece")
    project = pieces[_PROJECT_PIECE]
    if set(project) != set(_PROJECT_SECTIONS):
        raise ValueError(f"the {_PROJECT_PIECE} piece holds {sorted(project)}")
    surface = {**project, **{section: {} for section in _PATH_SECTIONS}}
    for name, piece in pieces.items():
        if name == _PROJECT_PIECE:
            continue
        if not piece or not set(piece) <= set(_PATH_SECTIONS):
            raise ValueError(f"piece {name} holds {sorted(piece)}")
        for section, entries in piece.items():
            if not entries:
                raise ValueError(f"piece {name} holds an empty {section}")
            for path, value in entries.items():
                if _piece_name(path) != name:
                    raise ValueError(
                        f"{path} is recorded in piece {name}, which is not {_piece_name(path)}"
                    )
                surface[section][path] = value
    return surface


def _read_snapshot(directory=SNAPSHOT_DIR):
    """Read every piece in *directory*, keyed by its file name without `.json`."""
    pieces = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    }
    if not pieces:
        raise FileNotFoundError(f"no snapshot pieces in {directory}")
    return pieces


def _write_snapshot(surface, directory=SNAPSHOT_DIR):
    """Write *surface* as pieces into *directory*, removing pieces it no longer has."""
    pieces = _split(surface)
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.json"):
        if stale.stem not in pieces:
            stale.unlink()
    for name, piece in pieces.items():
        text = json.dumps(piece, indent=2, sort_keys=True) + "\n"
        (directory / f"{name}.json").write_text(text, encoding="utf-8")


def test_declared_compatibility_surface_is_recorded():
    surface = _surface(ROOT)
    recorded = _read_snapshot()
    # A piece missing or left over is named here, before the contents are compared.
    missing = sorted(set(_split(surface)) - set(recorded))
    extra = sorted(set(recorded) - set(_split(surface)))
    assert not missing and not extra, (
        f"snapshot pieces missing: {missing}; pieces the surface no longer has: {extra}; "
        "regenerate with `python tests/contracts/test_boundary_compatibility.py`"
    )
    assert _join(recorded) == surface


def test_the_pieces_assemble_into_the_surface_they_were_split_from():
    surface = _surface(ROOT)
    assert _join(_split(surface)) == surface
    assert _join(json.loads(json.dumps(_split(surface)))) == surface


def test_a_module_goes_to_its_package_and_first_subpackage():
    assert _piece_name("lionagi/__init__.py") == "lionagi"
    assert _piece_name("lionagi/ln/concurrency/primitives.py") == "lionagi.ln"
    assert _piece_name("lionhub/src/lionhub/providers/openai/codex.py") == "lionhub.providers"
    assert _piece_name("apps/studio/backend/src/lionagi_studio/api/app.py") == "lionagi_studio.api"
    with pytest.raises(ValueError, match="no package root"):
        _piece_name("scripts/ci.py")


def _small_surface():
    return {
        "package_exports": {"lionagi/__init__.py": ["__all__ = ['a']"]},
        "public_definitions": {
            "lionagi/ln/core.py": ["run"],
            "lionhub/src/lionhub/providers/codex.py": ["stream"],
        },
        "environment_lookups": {"lionhub/src/lionhub/providers/codex.py": ["os.getenv:'X'"]},
        "moved_modules": None,
        "settings_declarations": [],
        "extras": {},
        "console_scripts": {"li": "a:b"},
    }


@pytest.mark.parametrize(
    "damage",
    ["no-project", "project-section-missing", "wrong-piece", "unknown-section", "empty-section"],
)
def test_a_piece_out_of_shape_fails_the_assembly(damage):
    pieces = _split(_small_surface())
    if damage == "no-project":
        del pieces[_PROJECT_PIECE]
    elif damage == "project-section-missing":
        del pieces[_PROJECT_PIECE]["extras"]
    elif damage == "wrong-piece":
        pieces["lionagi"]["public_definitions"] = pieces.pop("lionagi.ln")["public_definitions"]
    elif damage == "unknown-section":
        pieces["lionagi"]["extras"] = {"lionagi/x.py": []}
    else:
        pieces["lionagi"]["public_definitions"] = {}
    with pytest.raises(ValueError):
        _join(pieces)


def test_a_missing_or_extra_piece_on_disk_is_named(tmp_path):
    surface = _small_surface()
    _write_snapshot(surface, tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "lionagi.json",
        "lionagi.ln.json",
        "lionhub.providers.json",
        "project.json",
    ]
    assert _join(_read_snapshot(tmp_path)) == surface
    (tmp_path / "lionagi.ln.json").unlink()
    (tmp_path / "lionagi.old.json").write_text("{}\n", encoding="utf-8")
    assert sorted(_read_snapshot(tmp_path)) != sorted(_split(surface))
    _write_snapshot(surface, tmp_path)
    assert not (tmp_path / "lionagi.old.json").exists()
    assert sorted(_read_snapshot(tmp_path)) == sorted(_split(surface))


def test_an_empty_snapshot_directory_is_not_an_empty_record(tmp_path):
    with pytest.raises(FileNotFoundError):
        _read_snapshot(tmp_path)


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
        "old": ("new.api.call", "2.0.0"),
        "keep": ("new.api.keep", "2.0.0"),
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
    aliases["extra"] = ("new.api.other", "2.0.0")
    write(aliases)
    assert _surface(tmp_path)["public_definitions"] != original["public_definitions"]
    del aliases["extra"]
    aliases["old"] = ("new.api.changed", "2.0.0")
    write(aliases)
    assert _surface(tmp_path)["package_exports"] != original["package_exports"]
    aliases["old"] = ("new.api.call", "3.0.0")
    write(aliases)
    assert _surface(tmp_path)["package_exports"] != original["package_exports"]
    aliases.clear()
    write(aliases)
    assert "lionagi/api.py" not in _surface(tmp_path)["public_definitions"]


@pytest.mark.parametrize(
    "source",
    [
        "def __getattr__(name):\n    return MISSING.get(name)\n",
        "FORWARDS = dict(old=('new.api.call', '2.0.0'))\n"
        "def __getattr__(name):\n    return FORWARDS.get(name)\n",
        "_DEPRECATED_ALIASES = {'old': ('new.api.call', '2.0.0')}\n"
        "def __getattr__(name):\n    raise AttributeError(name)\n",
        "_DEPRECATED_ALIASES = {'old': ('new.api.call', '2.0.0')}\n"
        "def __getattr__(name):\n    _DEPRECATED_ALIASES = {}\n"
        "    return _DEPRECATED_ALIASES.get(name)\n",
        "FIELDS = {'old': ('field', {})}\ndef __getattr__(name):\n    return FIELDS.get(name)\n",
        "FORWARDS = {'old': 'new.api.call'}\n"
        "def __getattr__(name):\n    return FORWARDS.get(name)\n",
        "FORWARDS = {'old': ('new.api.call', '2.0.0')}\n"
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


def test_surface_extraction_reads_hub_modules(tmp_path):
    package = tmp_path / "lionagi"
    package.mkdir()
    (package / "config.py").write_text("class AppSettings:\n    pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    hub = tmp_path / "lionhub/src/lionhub/orchestration"
    hub.mkdir(parents=True)
    module = hub / "service.py"
    module.write_text('def run():\n    return os.environ.get("EXAMPLE_ROOT")\n', encoding="utf-8")
    surface = _surface(tmp_path)
    key = "lionhub/src/lionhub/orchestration/service.py"
    assert surface["environment_lookups"][key] == ['os.environ.get:"EXAMPLE_ROOT"']
    assert surface["public_definitions"][key] == ["run"]
    module.write_text('def run():\n    return os.environ.get("OTHER_ROOT")\n', encoding="utf-8")
    assert _surface(tmp_path)["environment_lookups"] != surface["environment_lookups"]


if __name__ == "__main__":
    _write_snapshot(_surface(ROOT))
