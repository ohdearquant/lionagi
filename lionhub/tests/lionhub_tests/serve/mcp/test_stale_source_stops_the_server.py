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
import sys
from pathlib import Path

import pytest

from lionhub.orchestration.job import EXIT_CODE_ENVIRONMENT_ERROR
from lionhub.serve.mcp import dispatch

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
                    elif node.module:
                        found.add(f"lionhub.serve.mcp.{node.module}")
                    else:
                        found.update(f"lionhub.serve.mcp.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".", 1)[0] in ("lionagi", "lionhub")}


def test_every_module_the_server_imports_inside_a_function_is_preloaded():
    imports = _function_level_own_imports()
    assert "lionhub.serve.cli.machine" in imports, "the scan must see the known lazy imports"
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
