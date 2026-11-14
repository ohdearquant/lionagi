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

import pytest

ROOT = Path(__file__).resolve().parents[5]
CORE = ROOT / "lionagi"
CATALOG = ROOT / "lionhub" / "src" / "lionhub" / "serve" / "cli" / "catalog.py"


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
            prefixes = (
                "lionagi.cli",
                "lionagi.studio.cli",
                "lionhub.agent.casts.surfaces",
                "lionhub.serve.cli.studio",
                "lionhub.serve.cli.schedule",
            )
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


def test_catalog_uses_only_the_private_marker_reader():
    assert _private_auto_imports(CATALOG.read_text()) == ["_read_registration_marker"]
    assert _private_auto_imports("from lionagi._auto import _MARKER_ATTR as marker") == [
        "_MARKER_ATTR"
    ]


def test_core_catalog_guard_sees_construction_and_target_literals():
    probe = CORE / "state" / "_probe.py"
    source = (
        "from lionagi._auto import CliSeed as Seed\n"
        "item = Seed(name='x', help='h', module='lionagi.cli.agent')\n"
        "module = 'lionhub.agent.casts.surfaces'\n"
        "other = auto.CliSeed(name='x', help='h', module='lionagi.studio.cli')\n"
    )
    hits = _core_catalog_references(probe, source)
    assert sorted(line for line, _ in hits) == [2, 2, 3, 4, 4]
    assert _core_catalog_references(CORE / "_log_channels.py", "name = 'lionagi.cli.warn'") == []
    assert _core_catalog_references(probe, "logging.getLogger('lionagi.cli')") == []
    assert _core_catalog_references(probe, "target = 'lionagi.cli'") == [(1, "lionagi.cli")]
    assert _core_catalog_references(probe, "target = 'lionhub.serve.cli.schedule'") == [
        (1, "lionhub.serve.cli.schedule")
    ]


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
from lionhub.serve.cli.catalog import iter_cli_seeds, seed_for, command_exists
seeds = iter_cli_seeds()
modules = {s.name: s.module for s in seeds}
assert modules['studio'] == 'lionhub.serve.cli.studio'
assert modules['schedule'] == 'lionhub.serve.cli.schedule'
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

    handlers = _fresh("""
import json
from lionhub.serve.cli.catalog import load_cli_command, seed_for
print(json.dumps({name: load_cli_command(seed_for(name)).handler.__module__
                  for name in ('studio', 'schedule')}))
""")
    assert json.loads(handlers) == {
        "studio": "lionhub.serve.cli.studio",
        "schedule": "lionhub.serve.cli.schedule",
    }


def test_catalog_loading_stays_lazy():
    _fresh("""
import sys
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
from lionhub.serve.cli import catalog
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
    for selected, other in (("schedule", "studio"), ("studio", "schedule")):
        _fresh(f"""
import sys
from lionhub.serve.cli import catalog
seed = catalog.seed_for({selected!r})
catalog.load_cli_command(seed)
assert {("lionhub.serve.cli." + other)!r} not in sys.modules
assert 'lionagi.studio.cli' not in sys.modules
if {selected!r} == 'schedule':
    assert 'fastapi' not in sys.modules
    assert 'uvicorn' not in sys.modules
    assert 'lionhub.orchestration.scheduler.engine' not in sys.modules
""")

    _fresh("""
import importlib
main = importlib.import_module('lionagi.cli.main')
from lionhub.serve.cli import schedule
schedule.run_schedule_quick_create = lambda kind, argv: (kind, argv)
assert main.main(['schedule', 'create', 'agent', 'sample']) == ('agent', ['sample'])
import sys
assert 'lionagi.studio.cli' not in sys.modules
""")


def test_adapter_import_boundaries_in_fresh_processes():
    for module, forbidden in (
        (
            "lionhub.serve.cli.schedule",
            (
                "lionagi.studio.cli",
                "lionhub.serve.cli.studio",
                "apps.studio",
                "lionagi.studio.app",
                "lionagi.studio.routes",
                "lionhub.orchestration.scheduler.engine",
                "fastapi",
                "uvicorn",
            ),
        ),
        ("lionhub.serve.cli.studio", ("lionagi.studio.cli", "lionhub.serve.cli.schedule")),
    ):
        source = f"""
import importlib.abc, sys
blocked = {forbidden!r}
attempts = []
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            attempts.append(fullname)
            raise AssertionError('forbidden import attempted: ' + fullname)
sys.meta_path.insert(0, Refuse())
__import__({module!r})
assert attempts == []
assert all(name not in sys.modules for name in blocked)
"""
        _fresh(source)


def test_cli_move_preserves_behavior():
    """Parser expectations were captured from the base commit's li commands."""
    from lionhub.serve.cli.catalog import build_cli_parser, seed_for

    studio_build = build_cli_parser(seed_for("studio"))
    schedule_build = build_cli_parser(seed_for("schedule"))
    choices = studio_build.parser._subparsers._group_actions[0].choices
    studio = choices["studio"]
    start = studio._subparsers._group_actions[0].choices["start"]
    schedule = schedule_build.parser._subparsers._group_actions[0].choices["schedule"]
    schedule_choices = schedule._subparsers._group_actions[0].choices

    assert tuple(schedule_choices) == (
        "list",
        "get",
        "limits",
        "create",
        "validate",
        "apply",
        "export",
        "enable",
        "disable",
        "delete",
        "trigger",
        "runs",
        "run",
        "status",
    )
    assert "--port PORT" in studio.format_help()
    assert "--host HOST" in studio.format_help()
    assert "--frontend-port FRONTEND_PORT" in studio.format_help()
    # The group's options and their order, read from the parser: how argparse wraps the usage
    # line differs between Python versions and terminal widths.
    assert [
        [action.option_strings for action in group._group_actions]
        for group in studio._mutually_exclusive_groups
    ] == [[["--web"], ["--docker"], ["--no-frontend"], ["--dev"]]]
    assert "--json" in schedule_choices["validate"].format_help()
    assert "--dry-run" in schedule_choices["apply"].format_help()
    assert "--output" in schedule_choices["export"].format_help()
    studio_defaults = studio.parse_args([])
    assert (studio_defaults.port, studio_defaults.host, studio_defaults.frontend_port) == (
        None,
        "127.0.0.1",
        3000,
    )
    start_defaults = studio.parse_args(["start"])
    assert (start_defaults.port, start_defaults.host, start_defaults.frontend_port) == (
        None,
        "127.0.0.1",
        3000,
    )
    assert schedule_choices["validate"].parse_args(["file.yaml"]).as_json is False

    def usage_words_and_body(text):
        # argparse wraps the usage line differently across Python versions, so the usage
        # is compared word by word and the rest of the help exactly.
        usage, _, body = text.partition("\n\n")
        return " ".join(usage.split()), body

    assert usage_words_and_body(start.format_help()) == usage_words_and_body(
        """usage: li studio start [-h] [--port PORT] [--host HOST]
                       [--frontend-port FRONTEND_PORT] [--no-open]
                       [--web | --docker | --no-frontend | --dev]

options:
  -h, --help            show this help message and exit
  --port PORT           Backend API port (default: LIONAGI_STUDIO_PORT env or
                        8765)
  --host HOST           Host to bind (default: 127.0.0.1). --docker also
                        publishes the container port on this host, so anything
                        else there exposes the daemon to that interface. This
                        is an IP address, not an address with a port: the port
                        is --port, and a value carrying a port is refused.
  --frontend-port FRONTEND_PORT
                        Frontend port (default: 3000)
  --no-open             Don't open the hosted UI in a browser (--web only)
  --web                 Start the backend only; frontend is the hosted UI
                        (default)
  --docker              Run the bundled frontend + backend via Docker. The
                        port is published on --host (loopback by default)
  --no-frontend         Only start the backend API server
  --dev                 Run the in-repo frontend in dev mode (hot-reload, no
                        build step)
"""
    )
    assert usage_words_and_body(schedule.format_help()) == usage_words_and_body(
        """usage: li schedule [-h]
                   {list,get,limits,create,validate,apply,export,enable,disable,delete,trigger,runs,run,status}
                   ...

Create, list, enable, disable, trigger, and delete schedules via the Studio
API (default http://127.0.0.1:8765). Set LIONAGI_STUDIO_URL to use a different
base URL.

positional arguments:
  {list,get,limits,create,validate,apply,export,enable,disable,delete,trigger,runs,run,status}
    list                List all schedules.
    get                 Show schedule details.
    limits              Show the global concurrent-fire cap and current in-
                        flight count.
    create              Create a new schedule.
    validate            Validate + statically resolve a ScheduleSet file (no
                        database writes).
    apply               Reconcile a ScheduleSet file into the database,
                        atomically.
    export              Convert schedules into a ScheduleSet document (never
                        writes the database).
    enable              Enable a schedule.
    disable             Disable a schedule.
    delete              Delete a schedule.
    trigger             Fire a schedule immediately.
    runs                List runs for a schedule.
    run                 Show one schedule run.
    status              "Did it work?" summary for a schedule.

options:
  -h, --help            show this help message and exit
"""
    )


def test_cli_version_source_is_unchanged(capsys):
    """The base commit printed this version through the composition root."""
    from lionagi.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "li 0.37.0\n"


def test_optional_command_feature_errors_are_unchanged(monkeypatch, capsys):
    """The missing-uvicorn message was captured from the base launcher body."""
    import builtins
    from argparse import Namespace

    from lionhub.serve.cli import studio

    original_import = builtins.__import__

    def missing_uvicorn(name, *args, **kwargs):
        if name == "uvicorn" or name.startswith("uvicorn."):
            raise ModuleNotFoundError("No module named 'uvicorn'", name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_uvicorn)
    assert studio._studio_start(Namespace(no_frontend=True)) == 1
    assert capsys.readouterr().err == (
        "uvicorn is required. Install with: pip install 'lionagi[studio]'\n"
    )


def test_cli_projection_uses_one_catalog():
    _fresh("""
import sys
assert not any(m == 'lionhub' or m.startswith('lionhub.') for m in sys.modules)
from lionhub.serve.cli import catalog
from lionhub.serve.mcp import projection
from lionagi.cli import machine
import importlib
main = importlib.import_module('lionagi.cli.main')
seed = catalog.seed_for('casts')
token = 'new-catalog-alias'
catalog._cli_seed_by_token[token] = seed
assert main.seed_for(token) is seed
assert projection._spec_for(token) is seed
assert catalog.seed_for('studio').module == 'lionhub.serve.cli.studio'
assert projection._spec_for('studio').module == 'lionhub.serve.cli.studio'
assert catalog.seed_for('schedule').module == 'lionhub.serve.cli.schedule'
assert projection._spec_for('schedule').module == 'lionhub.serve.cli.schedule'
try:
    machine._run_machine_command(['schedule', 'not-a-verb'])
except machine.MachineError as exc:
    assert exc.kind == 'invalid_input'
else:
    raise AssertionError('schedule machine classification stopped using its machine adapter')
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
    assert name not in vars(auto), name
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
