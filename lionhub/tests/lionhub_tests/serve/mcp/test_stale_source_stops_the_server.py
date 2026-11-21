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
import json
import subprocess
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


def _lazy_own_imports(module: str) -> set[str]:
    """The lionagi or lionhub modules that *module* imports inside its functions."""
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return set()
    is_package = spec.submodule_search_locations is not None
    package = module if is_package else module.rsplit(".", 1)[0]
    found: set[str] = set()
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    base = node.module or ""
                else:
                    parts = package.split(".")
                    base = ".".join(parts[: len(parts) - (node.level - 1)])
                    if node.module:
                        base = f"{base}.{node.module}"
                found.add(base)
                found.update(_submodules(base, node.names))
    return {name for name in found if name.split(".", 1)[0] in dispatch._OWN_PACKAGES}


def _lazy_import_closure() -> set[str]:
    """Every own module reachable from the server's modules through imports made inside
    functions, followed through each module it reaches."""
    pending = [
        f"lionhub.serve.mcp.{path.stem}"
        for path in sorted(_MCP_PACKAGE.glob("*.py"))
        if path.stem not in {"__init__", "__main__"}
    ]
    pending.extend(dispatch._PRELOAD_MODULES)
    seen: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_lazy_own_imports(module) - seen)
    return seen


def _submodules(package: str, names: list[ast.alias]) -> set[str]:
    """The names in ``from package import name, ...`` that are modules of the package."""
    if package.split(".", 1)[0] not in dispatch._OWN_PACKAGES:
        return set()
    spec = importlib.util.find_spec(package)
    if spec is None or spec.submodule_search_locations is None:
        return set()
    candidates = (f"{package}.{alias.name}" for alias in names)
    return {name for name in candidates if importlib.util.find_spec(name) is not None}


def test_every_module_the_verbs_import_inside_a_function_is_loaded_by_preload():
    """Followed through function-level imports: a module the session service imports lazily
    can itself import another one lazily, and the server must hold both before it answers.
    A module reached only through a top-level import is not scanned for its own lazy imports;
    a failure there after start ends the server through the request path instead."""
    closure = _lazy_import_closure()
    assert "lionhub.serve.cli.machine" in closure, "the scan must see the known lazy imports"
    assert "lionhub.serve.daemon.services.runs" in closure, "and relative ones"
    assert "lionhub.orchestration.scheduler.liveness" in closure, "and ones two levels down"

    script = (
        "import json, sys\n"
        "from lionhub.serve.mcp import dispatch\n"
        "dispatch.preload()\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300, check=True
    )
    loaded = set(json.loads(completed.stdout.strip().splitlines()[-1]))
    assert sorted(closure - loaded - {"lionhub.serve.mcp.server"}) == []


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


def test_preload_leaves_a_module_that_fails_for_another_reason_to_its_verbs(exits, monkeypatch):
    """A module that rejects its configuration while importing does not stop the server."""
    loaded = []

    def rejects_its_settings(name):
        if name == "lionagi.state.db":
            raise PermissionError("cannot read the settings file")
        loaded.append(name)

    monkeypatch.setattr(dispatch, "import_module", rejects_its_settings)

    dispatch.preload()

    assert loaded == [name for name in dispatch._PRELOAD_MODULES if name != "lionagi.state.db"]


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


class _SessionServiceRaising:
    def __init__(self, exc: ImportError) -> None:
        self.exc = exc

    async def get_session(self, _session_id):
        raise self.exc


async def test_run_detail_raises_when_reading_the_session_fails_to_import_its_own_code(
    one_recorded_session, monkeypatch
):
    """The session service imports more of this server's code while it reads a session."""
    import lionhub.serve.daemon.services as services

    stale = ImportError("cannot import name '_detect_status'", name=f"{services.__name__}.runs")
    monkeypatch.setattr(services, "sessions", _SessionServiceRaising(stale), raising=False)

    with pytest.raises(ImportError) as raised:
        await _run_detail.build_run_detail("run-1")
    assert raised.value is stale


class _SessionServiceReturning:
    def __init__(self, session: dict) -> None:
        self.session = session

    async def get_session(self, _session_id):
        return self.session


async def test_run_detail_raises_when_building_nodes_fails_to_import_its_own_code(
    one_recorded_session, monkeypatch
):
    """The node reshape imports the checkpoint reader; its failure is not malformed detail."""
    import lionhub.serve.daemon.services as services

    session = {"branches": [{"name": "worker"}]}
    monkeypatch.setattr(services, "sessions", _SessionServiceReturning(session), raising=False)
    monkeypatch.setitem(sys.modules, "lionhub.orchestration.flow.checkpoint", None)

    with pytest.raises(ImportError) as raised:
        await _run_detail.build_run_detail("run-1")
    assert raised.value.name == "lionhub.orchestration.flow.checkpoint"


async def test_run_detail_reports_a_session_read_that_needs_a_missing_package_as_unreadable(
    one_recorded_session, monkeypatch
):
    import lionhub.serve.daemon.services as services

    missing = ModuleNotFoundError("No module named 'starlette'", name="starlette")
    monkeypatch.setattr(services, "sessions", _SessionServiceRaising(missing), raising=False)

    assert await _run_detail.build_run_detail("run-1") == {
        "detail_unavailable": "session_detail_unreadable"
    }


def test_preload_names_a_failed_module_without_its_error_text(exits, monkeypatch, caplog):
    """A settings error can echo the rejected value, so only the exception type is logged."""

    def rejects_its_settings(name):
        if name == "lionagi.state.db":
            raise ValueError("input_value='sk-canary-value'")

    monkeypatch.setattr(dispatch, "import_module", rejects_its_settings)

    with caplog.at_level("WARNING"):
        dispatch.preload()

    assert "lionagi.state.db" in caplog.text
    assert "ValueError" in caplog.text
    assert "canary" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_preload_ends_the_server_when_its_own_source_no_longer_parses(exits, monkeypatch):
    own_file = str(Path(dispatch.__file__).with_name("verbs.py"))

    def broken(name):
        if name == "lionagi.state.db":
            raise SyntaxError("invalid syntax", (own_file, 1, 1, "x", 1, 2))

    monkeypatch.setattr(dispatch, "import_module", broken)

    with pytest.raises(_Exited) as exited:
        dispatch.preload()
    assert exited.value.code == EXIT_CODE_ENVIRONMENT_ERROR


async def test_a_request_that_meets_own_source_that_no_longer_parses_ends_the_server(
    exits, monkeypatch, capsys
):
    """An own module first imported after start, by a verb, that no longer parses."""
    own_file = str(Path(dispatch.__file__).with_name("verbs.py"))

    async def broken(ops, help):  # noqa: A002
        raise SyntaxError("invalid syntax", (own_file, 1, 1, "x", 1, 2))

    monkeypatch.setattr(dispatch, "_request", broken)

    with pytest.raises(_Exited) as exited:
        await dispatch.request(ops=[{"op": "server.info", "args": {}}])
    assert exited.value.code == EXIT_CODE_ENVIRONMENT_ERROR
    assert "verbs.py" in capsys.readouterr().err


async def test_a_request_that_meets_a_syntax_error_in_another_package_raises_it(
    exits, monkeypatch, tmp_path
):
    foreign_file = str(tmp_path / "elsewhere.py")

    async def broken(ops, help):  # noqa: A002
        raise SyntaxError("invalid syntax", (foreign_file, 1, 1, "x", 1, 2))

    monkeypatch.setattr(dispatch, "_request", broken)

    with pytest.raises(SyntaxError):
        await dispatch.request(ops=[{"op": "server.info", "args": {}}])


async def test_a_catalog_schema_whose_own_source_no_longer_parses_ends_the_server(
    exits, monkeypatch
):
    own_file = str(Path(dispatch.__file__).with_name("verbs.py"))

    def broken(verb, **_kwargs):
        raise SyntaxError("invalid syntax", (own_file, 1, 1, "x", 1, 2))

    monkeypatch.setattr(dispatch, "verb_schema", broken)

    with pytest.raises(_Exited) as exited:
        await dispatch.request(help=True)
    assert exited.value.code == EXIT_CODE_ENVIRONMENT_ERROR


def test_preload_leaves_a_syntax_error_in_another_package_to_its_verbs(
    exits, monkeypatch, tmp_path
):
    foreign_file = str(tmp_path / "elsewhere.py")

    def broken(name):
        if name == "lionagi.state.db":
            raise SyntaxError("invalid syntax", (foreign_file, 1, 1, "x", 1, 2))

    monkeypatch.setattr(dispatch, "import_module", broken)

    dispatch.preload()
