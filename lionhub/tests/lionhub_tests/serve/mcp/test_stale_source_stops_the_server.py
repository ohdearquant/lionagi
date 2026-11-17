# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A server whose own modules no longer import exits instead of serving part of its verbs.

The installed source can change under a running server, for example when an editable
checkout moves to a commit where a module has a new path. The server loads every module
it uses before it serves, and a failure to import one of its own modules after that ends
the process with a message naming the module, so the client starts a fresh server.
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import sys
from pathlib import Path

import pytest

from lionhub.orchestration.job import EXIT_CODE_ENVIRONMENT_ERROR
from lionhub.serve.mcp import _run_detail, dispatch

_MCP_PACKAGE = Path(dispatch.__file__).resolve().parent


class _Exited(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(code)


@pytest.fixture
def exits(monkeypatch):
    def fake_exit(code: int) -> None:
        raise _Exited(code)

    monkeypatch.setattr(dispatch, "_exit", fake_exit)


def _function_level_own_imports() -> set[str]:
    """Every lionagi or lionhub module the server package imports inside a function."""
    found: set[str] = set()
    for path in sorted(_MCP_PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Import):
                    found.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0:
                        found.add(node.module or "")
                        found.update(_submodules(node.module or "", node.names))
                    elif node.module:
                        found.add(f"lionhub.serve.mcp.{node.module}")
                    else:
                        found.update(f"lionhub.serve.mcp.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".", 1)[0] in dispatch._OWN_PACKAGES}


def _submodules(package: str, names: list[ast.alias]) -> set[str]:
    """The names in ``from package import name, ...`` that are modules of the package."""
    if package.split(".", 1)[0] not in dispatch._OWN_PACKAGES:
        return set()
    spec = importlib.util.find_spec(package)
    if spec is None or spec.submodule_search_locations is None:
        return set()
    candidates = (f"{package}.{alias.name}" for alias in names)
    return {name for name in candidates if importlib.util.find_spec(name) is not None}


def test_every_module_the_server_imports_inside_a_function_is_preloaded():
    imports = _function_level_own_imports()
    assert "lionhub.serve.cli.machine" in imports, "the scan must see the known lazy imports"
    assert "lionhub.serve.daemon.services.sessions" in imports, (
        "the scan must see a module imported by name from its package"
    )
    assert imports <= set(dispatch._PRELOAD_MODULES), sorted(
        imports - set(dispatch._PRELOAD_MODULES)
    )


async def test_a_verb_whose_module_no_longer_imports_ends_the_server(exits, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "lionhub.serve.cli.machine", None)

    with pytest.raises(_Exited) as exited:
        await dispatch.request(ops=[{"op": "server.info", "args": {}}])

    assert exited.value.code == EXIT_CODE_ENVIRONMENT_ERROR
    err = capsys.readouterr().err
    assert "lionhub.serve.cli.machine cannot be imported" in err
    assert "fresh server" in err


async def test_a_catalog_schema_that_fails_on_an_own_module_ends_the_server(exits, monkeypatch):
    def gone(verb, **_kwargs):
        raise ModuleNotFoundError(
            "No module named 'lionagi.cli.machine'", name="lionagi.cli.machine"
        )

    monkeypatch.setattr(dispatch, "verb_schema", gone)

    with pytest.raises(_Exited) as exited:
        await dispatch.request(help=True)
    assert exited.value.code == EXIT_CODE_ENVIRONMENT_ERROR


async def test_a_schema_that_needs_a_missing_package_is_reported_on_its_entry(exits, monkeypatch):
    """A package that is not installed is not a stale server: the entry says so and the rest
    of the catalog is served."""
    real = dispatch.verb_schema

    def needs_an_extra(verb, **kwargs):
        if verb.name == "server.info":
            raise ModuleNotFoundError("No module named 'some_extra'", name="some_extra")
        return real(verb, **kwargs)

    monkeypatch.setattr(dispatch, "verb_schema", needs_an_extra)

    answer = await dispatch.request(help=True)

    entry = next(e for e in answer["verbs"] if e["verb"] == "server.info")
    assert entry["available"] is False
    assert "some_extra" in entry["reason"]


async def test_with_every_module_importable_the_full_catalog_is_served(exits):
    answer = await dispatch.request(help=True)

    assert not [e for e in answer["verbs"] if "reason" in e]
    assert answer["available_count"] == len(
        [e for e in answer["verbs"] if e.get("available", True)]
    )


def test_preload_ends_the_server_when_one_of_its_modules_does_not_import(exits, monkeypatch):
    monkeypatch.setitem(sys.modules, "lionhub.serve.diagnostics", None)

    with pytest.raises(_Exited) as exited:
        dispatch.preload()
    assert exited.value.code == EXIT_CODE_ENVIRONMENT_ERROR


def test_preload_leaves_a_module_that_needs_a_missing_package_to_its_verbs(exits, monkeypatch):
    real = dispatch.import_module

    def missing_extra(name):
        if name == "lionhub.serve.daemon.services":
            raise ModuleNotFoundError("No module named 'some_extra'", name="some_extra")
        return real(name)

    monkeypatch.setattr(dispatch, "import_module", missing_extra)

    dispatch.preload()


class _OneRecordedSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get_sessions_for_run(self, _run_id):
        return [{"id": "session-1", "updated_at": 1.0}]


@pytest.fixture
def one_recorded_session(monkeypatch):
    monkeypatch.setattr("lionagi.state.db.StateDB", _OneRecordedSession)


async def test_run_detail_raises_when_its_own_session_service_does_not_import(
    one_recorded_session, monkeypatch
):
    """An own module that no longer imports is not reported as a missing extra."""
    import lionhub.serve.daemon.services as services

    # ``from services import sessions`` reads the package attribute first, so a module that
    # was already imported never fails; take it away so the import is attempted.
    monkeypatch.delattr(services, "sessions", raising=False)
    monkeypatch.setitem(sys.modules, "lionhub.serve.daemon.services.sessions", None)

    with pytest.raises(ImportError) as raised:
        await _run_detail.build_run_detail("run-1")
    assert raised.value.name == "lionhub.serve.daemon.services.sessions"


async def test_run_detail_reports_a_missing_studio_package_as_a_missing_extra(
    one_recorded_session, monkeypatch
):
    real_import = builtins.__import__

    def without_fastapi(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "lionhub.serve.daemon.services" and "sessions" in (fromlist or ()):
            raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_fastapi)

    assert await _run_detail.build_run_detail("run-1") == {
        "detail_unavailable": "studio_extra_not_installed"
    }
