# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cold-process contracts for the CLI catalog and its consumers."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "lionagi"
CATALOG = ROOT / "lionhub" / "src" / "lionhub" / "cli" / "catalog.py"


def _core_catalog_references(path: Path, source: str) -> list[tuple[int, str]]:
    """Find catalog construction or command-module literals in a core module."""
    tree = ast.parse(source, filename=str(path))
    seed_names = {"CliSeed"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "lionagi._auto":
            seed_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "CliSeed"
            )
    logger_names = {
        "lionagi.cli.progress",
        "lionagi.cli.hint",
        "lionagi.cli.warn",
        "lionagi.cli.error",
    }
    logger_arguments = {
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getLogger"
        and node.args
    }
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id in seed_names) or (
                isinstance(func, ast.Attribute) and func.attr == "CliSeed"
            ):
                hits.append((node.lineno, "CliSeed"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if id(node) in logger_arguments or (
                path == CORE / "_log_channels.py" and value in logger_names
            ):
                continue
            prefixes = ("lionagi.cli", "lionagi.studio.cli", "lionagi.casts.surfaces")
            if any(value == prefix or value.startswith(prefix + ".") for prefix in prefixes):
                hits.append((node.lineno, value))
    return hits


def _private_auto_imports(source: str) -> list[str]:
    return [
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "lionagi._auto"
        for alias in node.names
        if alias.name.startswith("_")
    ]


def test_catalog_uses_no_private_auto_imports():
    assert _private_auto_imports(CATALOG.read_text()) == []
    assert _private_auto_imports("from lionagi._auto import _MARKER_ATTR as marker") == [
        "_MARKER_ATTR"
    ]


def test_core_catalog_guard_sees_construction_and_target_literals():
    probe = CORE / "state" / "_probe.py"
    source = (
        "from lionagi._auto import CliSeed as Seed\n"
        "item = Seed(name='x', help='h', module='lionagi.cli.agent')\n"
        "module = 'lionagi.casts.surfaces'\n"
        "other = auto.CliSeed(name='x', help='h', module='lionagi.studio.cli')\n"
    )
    hits = _core_catalog_references(probe, source)
    assert sorted(line for line, _ in hits) == [2, 2, 3, 4, 4]
    assert _core_catalog_references(CORE / "_log_channels.py", "name = 'lionagi.cli.warn'") == []
    assert _core_catalog_references(probe, "logging.getLogger('lionagi.cli')") == []
    assert _core_catalog_references(probe, "target = 'lionagi.cli'") == [(1, "lionagi.cli")]


def _fresh(source: str, *args: str) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "lionhub" / "src"), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c", source, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_fresh_cli_catalog_discovery():
    source = """
import json, sys
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
from lionhub.cli.catalog import iter_cli_seeds, seed_for, command_exists
seeds = iter_cli_seeds()
assert seed_for('not-a-command') is None and not command_exists('not-a-command')
print(json.dumps({
    'top_level': [{'name': s.name, 'help': s.help, 'aliases': sorted(s.aliases)} for s in seeds],
    'name_map': {token: seed_for(token).name for s in seeds for token in (s.name, *s.aliases)},
}))
"""
    actual = json.loads(_fresh(source))
    expected = json.loads((ROOT / "tests/contracts/data/cli.json").read_text())
    assert actual["top_level"] == expected["top_level"]
    assert actual["name_map"] == expected["name_map"]
    assert len(actual["top_level"]) == 21

    # Each command starts with only the composition root imported, independently.
    source = """
import sys
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
from lionagi.cli.main import main
main([sys.argv[1], '--help'])
"""
    for name in ("schedule", "plugin", "casts", "studio"):
        assert name in _fresh(source, name).lower()


def test_catalog_loading_stays_lazy():
    _fresh("""
import sys
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
from lionhub.cli import catalog
seeds = catalog.iter_cli_seeds()
targets = {s.module for s in seeds}
assert not (targets & sys.modules.keys())
build = catalog.build_cli_parser(None)
assert build.registration is None and not catalog._cli_realized
assert not (targets & sys.modules.keys())
from lionagi import ln
real_import = ln.import_module
calls = []
def traced(name, *args, **kwargs):
    if name in targets:
        calls.append(name)
    return real_import(name, *args, **kwargs)
ln.import_module = traced
seed = catalog.seed_for('casts')
first = catalog.build_cli_parser(seed)
assert first.registration is catalog.load_cli_command(seed)
assert calls == [seed.module]
assert set(catalog._cli_realized) == {'casts'}
""")


def test_cli_projection_uses_one_catalog():
    _fresh("""
import sys
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
from lionhub.cli import catalog
from lionhub.serve.mcp import projection
from lionagi.cli import machine
import importlib
main = importlib.import_module('lionagi.cli.main')
seed = catalog.seed_for('casts')
token = 'new-catalog-alias'
catalog._cli_seed_by_token[token] = seed
assert main.seed_for(token) is seed
assert projection._spec_for(token) is seed
try:
    machine._run_machine_command([token])
except machine.MachineError as exc:
    assert exc.kind == 'unavailable'
else:
    raise AssertionError('machine classification ignored the catalog')
del catalog._cli_seed_by_token[token]
assert main.seed_for(token) is None
try:
    projection._spec_for(token)
except projection.SchemaProjectionError:
    pass
else:
    raise AssertionError('projection retained a copied catalog')
try:
    machine._run_machine_command([token])
except machine.MachineError as exc:
    assert exc.kind == 'invalid_input'
else:
    raise AssertionError('machine retained a copied catalog')
""")


def test_core_contains_no_cli_targets():
    _fresh("""
import importlib.abc, sys
class RefuseHub(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lionhub' or fullname.startswith('lionhub.'):
            raise AssertionError('core attempted to import lionhub')
sys.meta_path.insert(0, RefuseHub())
import lionagi._auto as auto
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
for name in ('_CLI_SEEDS', '_cli_seeds', '_cli_seed_by_token', '_cli_realized',
             'iter_cli_seeds', 'seed_for', 'command_exists', 'load_cli_command', 'build_cli_parser'):
    assert not hasattr(auto, name), name
""")
    files = [
        path
        for path in CORE.rglob("*.py")
        if path.relative_to(CORE).parts[0] not in {"cli", "studio"} and path != CORE / "_moved.py"
    ]
    assert CORE / "_auto.py" in files
    hits = {
        str(path.relative_to(ROOT)): found
        for path in files
        if (found := _core_catalog_references(path, path.read_text()))
    }
    assert hits == {}
