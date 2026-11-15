# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused contract tests for lionagi/_auto.py's declaration compiler."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import types
from contextlib import contextmanager

import pytest

from lionagi import _auto


@contextmanager
def _isolated_registries():
    with _auto._isolated_registry_for_tests():
        yield


def _make_module(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


@pytest.fixture(autouse=True)
def _cleanup_fake_modules():
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        del sys.modules[name]


# --- value objects ----------------------------------------------------------


def test_value_objects_are_frozen_and_slotted():
    http = _auto.HttpDeclaration(path="/x", method="GET")
    with pytest.raises(dataclasses.FrozenInstanceError):
        http.path = "/y"  # type: ignore[misc]
    assert not hasattr(http, "__dict__")

    seed = _auto.CliSeed(name="x", help="h", module="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        seed.name = "y"  # type: ignore[misc]
    assert not hasattr(seed, "__dict__")


def test_http_declaration_defaults():
    http = _auto.HttpDeclaration(path="/x", method="GET")
    assert http.tags is None
    assert http.dependencies == ()
    assert http.include_in_schema is True


# --- auto_register validation ------------------------------------------------


def test_auto_register_returns_handler_unchanged_and_attaches_marker():
    http = _auto.HttpDeclaration(path="/x", method="GET")

    def handler():
        return 1

    decorated = _auto.auto_register(area="widgets", http=http)(handler)
    assert decorated is handler
    marker = _auto._read_registration_marker(decorated)
    assert marker is getattr(decorated, _auto._MARKER_ATTR)
    assert marker.area == "widgets"
    assert marker.http is http
    assert marker.cli is None


def test_public_marker_reader_returns_none_for_unmarked_objects():
    assert _auto._read_registration_marker(object()) is None


def test_public_marker_reader_retains_cli_declaration():
    cli = _auto.CliDeclaration(seed="sample", parser_factory=lambda subparsers: None)

    @_auto.auto_register(area="sample-area", cli=cli)
    def handler():
        pass

    marker = _auto._read_registration_marker(handler)
    assert isinstance(marker, _auto.RegistrationMarker)
    assert marker.area == "sample-area" and marker.cli is cli and marker.http is None


def test_auto_register_requires_exactly_one_surface():
    with pytest.raises(_auto.InvalidRegistrationError):
        _auto.auto_register(area="widgets")

    http = _auto.HttpDeclaration(path="/x", method="GET")
    cli = _auto.CliDeclaration(seed="widgets", parser_factory=lambda subparsers: None)
    with pytest.raises(_auto.InvalidRegistrationError):
        _auto.auto_register(area="widgets", http=http, cli=cli)


def test_auto_register_rejects_empty_area():
    http = _auto.HttpDeclaration(path="/x", method="GET")
    with pytest.raises(_auto.InvalidRegistrationError):
        _auto.auto_register(area="", http=http)


def test_auto_register_rejects_invalid_method_and_empty_path():
    with pytest.raises(_auto.InvalidRegistrationError):
        _auto.auto_register(
            area="widgets",
            http=_auto.HttpDeclaration(path="/x", method="TRACE"),  # type: ignore[arg-type]
        )
    with pytest.raises(_auto.InvalidRegistrationError):
        _auto.auto_register(area="widgets", http=_auto.HttpDeclaration(path="", method="GET"))


def test_auto_register_rejects_empty_cli_seed():
    cli = _auto.CliDeclaration(seed="", parser_factory=lambda subparsers: None)
    with pytest.raises(_auto.InvalidRegistrationError):
        _auto.auto_register(area="widgets", cli=cli)


# --- HTTP compilation ---------------------------------------------------------


def test_http_load_compiles_in_fixed_module_and_definition_order(monkeypatch):
    mod_a = _make_module(
        "tests.contracts._fake_http_a",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', http=_auto.HttpDeclaration(path='/a1', method='GET'))\n"
        "def a1():\n    return 1\n"
        "@_auto.auto_register(area='a', http=_auto.HttpDeclaration(path='/a2', method='GET'))\n"
        "def a2():\n    return 2\n",
    )
    mod_b = _make_module(
        "tests.contracts._fake_http_b",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='b', http=_auto.HttpDeclaration(path='/b1', method='POST'))\n"
        "def b1():\n    return 3\n",
    )
    monkeypatch.setattr(_auto, "_HTTP_MODULES", (mod_a.__name__, mod_b.__name__))
    with _isolated_registries():
        _auto.load_http_modules()
        registrations = _auto.iter_http()
        assert [r.qualname for r in registrations] == ["a1", "a2", "b1"]
        assert [r.order for r in registrations] == [0, 1, 2]


def test_http_tags_default_to_area_when_unset_and_preserved_when_set(monkeypatch):
    mod = _make_module(
        "tests.contracts._fake_http_tags",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='widgets', http=_auto.HttpDeclaration(path='/w', method='GET'))\n"
        "def untagged():\n    return 1\n"
        "@_auto.auto_register(area='widgets', http=_auto.HttpDeclaration(path='/w2', method='GET', tags=('custom',)))\n"
        "def tagged():\n    return 2\n",
    )
    monkeypatch.setattr(_auto, "_HTTP_MODULES", (mod.__name__,))
    with _isolated_registries():
        _auto.load_http_modules()
        by_name = {r.qualname: r for r in _auto.iter_http()}
        assert by_name["untagged"].http.tags == ("widgets",)
        assert by_name["tagged"].http.tags == ("custom",)


def test_http_rescan_of_cached_module_is_idempotent(monkeypatch):
    mod = _make_module(
        "tests.contracts._fake_http_idempotent",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', http=_auto.HttpDeclaration(path='/a', method='GET'))\n"
        "def handler():\n    return 1\n",
    )
    monkeypatch.setattr(_auto, "_HTTP_MODULES", (mod.__name__,))
    with _isolated_registries():
        _auto.load_http_modules()
        _auto.load_http_modules()
        assert len(_auto.iter_http()) == 1


def test_http_duplicate_identity_with_different_callable_raises(monkeypatch):
    mod = _make_module(
        "tests.contracts._fake_http_dup",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', http=_auto.HttpDeclaration(path='/a', method='GET'))\n"
        "def handler():\n    return 1\n"
        "@_auto.auto_register(area='a', http=_auto.HttpDeclaration(path='/a', method='GET'))\n"
        "def handler2():\n    return 2\n",
    )
    # Force a colliding identity: same module/qualname/path/method, distinct objects.
    mod.handler2.__qualname__ = mod.handler.__qualname__
    monkeypatch.setattr(_auto, "_HTTP_MODULES", (mod.__name__,))
    with _isolated_registries():
        with pytest.raises(_auto.DuplicateRegistrationError):
            _auto.load_http_modules()


def test_http_scan_ignores_imported_callables_and_cli_only_markers(monkeypatch):
    source_mod = _make_module(
        "tests.contracts._fake_http_source",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', http=_auto.HttpDeclaration(path='/a', method='GET'))\n"
        "def local_http():\n    return 1\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='x', parser_factory=lambda s: None))\n"
        "def local_cli():\n    return 2\n",
    )
    importer_mod = _make_module(
        "tests.contracts._fake_http_importer",
        "from tests.contracts._fake_http_source import local_http, local_cli\n",
    )
    monkeypatch.setattr(_auto, "_HTTP_MODULES", (importer_mod.__name__,))
    with _isolated_registries():
        _auto.load_http_modules()
        assert _auto.iter_http() == ()


# --- import isolation boundary -------------------------------------------------


def test_retired_casts_cli_module_is_not_shipped():
    assert "lionhub.serve.cli.casts" not in sys.modules
    assert importlib.util.find_spec("lionhub.serve.cli.casts") is None
    assert importlib.util.find_spec("lionhub.agent.casts.surfaces") is not None


def test_auto_module_is_not_exported_from_root():
    import lionagi

    assert "_auto" not in lionagi.__all__
    assert all(target[0] != "_auto" for target in lionagi._LAZY_MAP.values())


def test_isolated_registry_for_tests_restores_previous_state():
    _auto._http.clear()
    _auto._http_keys.clear()
    sentinel = _auto.Registration(
        order=0, area="pre", module="m", qualname="q", handler=lambda: None
    )
    _auto._http.append(sentinel)
    try:
        with _isolated_registries():
            assert _auto.iter_http() == ()
            _auto._http.append(
                _auto.Registration(
                    order=0, area="inside", module="m", qualname="q2", handler=lambda: None
                )
            )
        assert _auto.iter_http() == (sentinel,)
    finally:
        _auto._http.clear()
        _auto._http_keys.clear()


def test_http_loader_delegates_to_shared_import_and_keeps_registration(monkeypatch):
    from lionagi import ln

    name = "tests.contracts._shared_import_http"
    module = _make_module(
        name,
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='shared-import', http=_auto.HttpDeclaration(path='/shared-import', method='GET'))\n"
        "def handler():\n    return 'handled'\n",
    )
    real_import = ln.import_module
    calls = []

    def shared_import(module_name, *args, **kwargs):
        calls.append((module_name, args, kwargs))
        return real_import(module_name, *args, **kwargs)

    monkeypatch.setattr(ln, "import_module", shared_import)
    with _isolated_registries():
        monkeypatch.setattr(_auto, "_HTTP_MODULES", (name,))
        _auto.load_http_modules()
        _auto.load_http_modules()
        registrations = _auto.iter_http()
        assert len(registrations) == 1
        assert registrations[0].handler is module.handler
        assert calls == [(name, (), {}), (name, (), {})]


@pytest.mark.parametrize("missing", [False, True], ids=["plain-error", "missing-module"])
def test_http_import_failure_preserves_shared_error_boundary(monkeypatch, tmp_path, missing):
    name = "auto_import_failure_http"
    carrier_name = "_auto_import_failure_values"
    if missing:
        failure = ModuleNotFoundError("required extra absent", name="auto_dep", path="dep.py")
    else:
        error_type = type("AutoDependencyError", (ImportError,), {})
        failure = error_type("required extra failed", name="auto_dep", path="dep.py")
    cause = ValueError("original dependency cause")
    failure.__cause__ = cause
    carrier = types.ModuleType(carrier_name)
    carrier.failure = failure
    monkeypatch.setitem(sys.modules, carrier_name, carrier)
    (tmp_path / f"{name}.py").write_text(f"from {carrier_name} import failure\nraise failure\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with _isolated_registries():
        with pytest.raises(ImportError) as raised:
            monkeypatch.setattr(_auto, "_HTTP_MODULES", (name, "never_imported_after_failure"))
            _auto.load_http_modules()
        assert _auto.iter_http() == ()
        assert "never_imported_after_failure" not in sys.modules
        if missing:
            assert raised.value is failure
            assert failure.name == "auto_dep" and failure.path == "dep.py"
            assert failure.__cause__ is cause
        else:
            assert type(raised.value) is ImportError
            assert str(raised.value) == f"Failed to import module {name}: required extra failed"
            assert raised.value.name is None and raised.value.path is None
            assert raised.value.__cause__ is failure
            assert failure.name == "auto_dep" and failure.path == "dep.py"
            assert failure.__cause__ is cause
