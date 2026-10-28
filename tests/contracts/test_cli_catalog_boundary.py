# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cold-process contracts for the CLI catalog and its consumers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
import ast, importlib.abc, pathlib, sys
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
tree = ast.parse(pathlib.Path(auto.__file__).read_text())
for node in ast.walk(tree):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        assert not node.value.startswith(('lionagi.cli.', 'lionagi.casts.', 'lionagi.studio.cli'))
""")
