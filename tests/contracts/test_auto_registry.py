# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused contract tests for lionagi/_auto.py's declaration compiler."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import sys
import types

import pytest

from lionagi import _auto

_EXPECTED_CLI_SEEDS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("orchestrate", ("o",), "lionagi.cli.orchestrate"),
    ("agent", (), "lionagi.cli.agent"),
    ("casts", (), "lionagi.casts.surfaces"),
    ("engine", (), "lionagi.cli.engine"),
    ("team", (), "lionagi.cli.team"),
    ("studio", (), "lionagi.studio.cli"),
    ("schedule", (), "lionagi.studio.cli"),
    ("state", (), "lionagi.cli.state"),
    ("invoke", (), "lionagi.cli.invoke"),
    ("kill", (), "lionagi.cli.kill"),
    ("mirror", (), "lionagi.cli.mirror"),
    ("monitor", ("mon",), "lionagi.cli.monitor"),
    ("dispatch", (), "lionagi.cli.dispatch"),
    ("doctor", (), "lionagi.cli.doctor"),
    ("stats", (), "lionagi.cli.stats"),
    ("plugin", (), "lionagi.cli.plugin"),
    ("hooks", (), "lionagi.cli.hooks"),
    ("handshake", (), "lionagi.cli.machine"),
    ("runs", (), "lionagi.cli.machine"),
    ("lifecycle", (), "lionagi.cli.machine"),
    ("mcp", (), "lionagi.cli.mcp"),
)


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
    marker = getattr(decorated, _auto._MARKER_ATTR)
    assert marker.area == "widgets"
    assert marker.http is http
    assert marker.cli is None


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


# --- CLI seed contract --------------------------------------------------------


def test_cli_seeds_are_fixed_21_in_order():
    seeds = _auto.iter_cli_seeds()
    assert len(seeds) == 21
    actual = tuple((s.name, s.aliases, s.module) for s in seeds)
    assert actual == _EXPECTED_CLI_SEEDS


def test_seed_for_and_command_exists_resolve_canonical_names_and_aliases():
    assert _auto.seed_for("orchestrate").name == "orchestrate"
    assert _auto.seed_for("o").name == "orchestrate"
    assert _auto.seed_for("mon").name == "monitor"
    assert _auto.seed_for("does-not-exist") is None
    assert _auto.command_exists("mcp") is True
    assert _auto.command_exists("does-not-exist") is False


def test_duplicate_seed_token_raises_duplicate_registration_error():
    a = _auto.CliSeed(name="alpha", help="a", module="m.a")
    b = _auto.CliSeed(name="beta", help="b", module="m.b", aliases=("alpha",))
    with pytest.raises(_auto.DuplicateRegistrationError):
        _auto._build_seed_by_token((a, b))


def test_duplicate_canonical_seed_name_raises_duplicate_registration_error():
    a = _auto.CliSeed(name="same", help="a", module="m.a")
    b = _auto.CliSeed(name="same", help="b", module="m.b")
    with pytest.raises(_auto.DuplicateRegistrationError):
        _auto._build_seed_by_token((a, b))


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
    with _auto._isolated_registry_for_tests():
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
    with _auto._isolated_registry_for_tests():
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
    with _auto._isolated_registry_for_tests():
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
    with _auto._isolated_registry_for_tests():
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
    with _auto._isolated_registry_for_tests():
        _auto.load_http_modules()
        assert _auto.iter_http() == ()


# --- CLI compilation -----------------------------------------------------------


def test_cli_load_command_imports_only_the_selected_module(monkeypatch):
    mod_a = _make_module(
        "tests.contracts._fake_cli_a",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='alpha', parser_factory=lambda s: None))\n"
        "def add_alpha_subparser():\n    return 1\n",
    )
    seed_a = _auto.CliSeed(name="alpha", help="h", module=mod_a.__name__)
    seed_b = _auto.CliSeed(
        name="beta", help="h", module="tests.contracts._nonexistent_should_not_import"
    )
    monkeypatch.setattr(_auto, "_cli_seeds", (seed_a, seed_b))
    monkeypatch.setattr(_auto, "_cli_seed_by_token", {"alpha": seed_a, "beta": seed_b})
    with _auto._isolated_registry_for_tests():
        registration = _auto.load_cli_command(seed_a)
        assert registration.handler is mod_a.add_alpha_subparser
        assert "tests.contracts._nonexistent_should_not_import" not in sys.modules


def test_cli_load_command_is_cached(monkeypatch):
    mod_a = _make_module(
        "tests.contracts._fake_cli_cache",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='alpha', parser_factory=lambda s: None))\n"
        "def handler():\n    return 1\n",
    )
    seed_a = _auto.CliSeed(name="alpha", help="h", module=mod_a.__name__)
    monkeypatch.setattr(_auto, "_cli_seeds", (seed_a,))
    with _auto._isolated_registry_for_tests():
        first = _auto.load_cli_command(seed_a)
        second = _auto.load_cli_command(seed_a)
        assert first is second


def test_cli_zero_matching_markers_raises_contract_error(monkeypatch):
    mod_a = _make_module("tests.contracts._fake_cli_zero", "x = 1\n")
    seed_a = _auto.CliSeed(name="alpha", help="h", module=mod_a.__name__)
    with _auto._isolated_registry_for_tests():
        with pytest.raises(_auto.RegistrationContractError):
            _auto.load_cli_command(seed_a)


def test_cli_multiple_matching_markers_raises_contract_error(monkeypatch):
    mod_a = _make_module(
        "tests.contracts._fake_cli_multi",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='alpha', parser_factory=lambda s: None))\n"
        "def one():\n    return 1\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='alpha', parser_factory=lambda s: None))\n"
        "def two():\n    return 2\n",
    )
    seed_a = _auto.CliSeed(name="alpha", help="h", module=mod_a.__name__)
    with _auto._isolated_registry_for_tests():
        with pytest.raises(_auto.RegistrationContractError):
            _auto.load_cli_command(seed_a)


def test_cli_marker_module_mismatch_raises_contract_error(monkeypatch):
    _make_module(
        "tests.contracts._fake_cli_origin",
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='alpha', parser_factory=lambda s: None))\n"
        "def handler():\n    return 1\n",
    )
    reexport_mod = _make_module(
        "tests.contracts._fake_cli_reexport",
        "from tests.contracts._fake_cli_origin import handler\n",
    )
    seed_a = _auto.CliSeed(name="alpha", help="h", module=reexport_mod.__name__)
    with _auto._isolated_registry_for_tests():
        with pytest.raises(_auto.RegistrationContractError):
            _auto.load_cli_command(seed_a)


# --- build_cli_parser ------------------------------------------------------------


def test_build_cli_parser_with_no_selection_registers_every_seed_as_stub():
    build = _auto.build_cli_parser(None)
    assert build.parser.prog == "li"
    assert build.seed is None
    assert build.registration is None
    assert build.selected_parser is None
    action_dests = {a.dest for a in build.parser._actions}
    assert "machine" in action_dests
    choices = build.parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert "orchestrate" in choices
    assert "o" in choices  # alias registered
    assert len(_auto.iter_cli_seeds()) == 21


def test_build_cli_parser_with_selection_uses_its_parser_factory(monkeypatch):
    captured: dict[str, object] = {}

    def factory(subparsers: argparse._SubParsersAction):
        p = subparsers.add_parser("alpha")
        captured["subparsers"] = subparsers
        return p

    mod_a = _make_module("tests.contracts._fake_cli_build", "")
    mod_a.factory = factory
    mod_a.handler = lambda args: 0
    marker_source = (
        "from lionagi import _auto\n"
        "from tests.contracts._fake_cli_build import factory\n"
        "@_auto.auto_register(area='a', cli=_auto.CliDeclaration(seed='alpha', parser_factory=factory))\n"
        "def add_alpha_subparser():\n    return 1\n"
    )
    marker_mod = _make_module("tests.contracts._fake_cli_build_marker", marker_source)
    seed_a = _auto.CliSeed(name="alpha", help="h", module=marker_mod.__name__)
    monkeypatch.setattr(_auto, "_cli_seeds", (seed_a,))
    monkeypatch.setattr(_auto, "_cli_seed_by_token", {"alpha": seed_a})
    with _auto._isolated_registry_for_tests():
        build = _auto.build_cli_parser(seed_a)
        assert build.seed is seed_a
        assert build.registration.handler is marker_mod.add_alpha_subparser
        assert isinstance(build.selected_parser, argparse.ArgumentParser)
        assert "subparsers" in captured


# --- isolation context manager ----------------------------------------------------


def test_isolated_registry_for_tests_restores_previous_state():
    # Start from a known-clean slate: an earlier test in this worker may have
    # dispatched a real `li <command>` invocation outside any isolation
    # context (C1 wires `lionagi.cli.main` to this module-global registry),
    # leaving compiled entries this test does not own. Only the "pre" entry
    # set below belongs to this test's own before/after assertions.
    _auto._http.clear()
    _auto._http_keys.clear()
    _auto._cli_realized.clear()
    sentinel_reg = _auto.Registration(
        order=0, area="pre", module="m", qualname="q", handler=lambda: None
    )
    _auto._http.append(sentinel_reg)
    _auto._cli_realized["pre"] = sentinel_reg
    try:
        with _auto._isolated_registry_for_tests():
            assert _auto.iter_http() == ()
            assert _auto._cli_realized == {}
            _auto._http.append(
                _auto.Registration(
                    order=0, area="inside", module="m", qualname="q2", handler=lambda: None
                )
            )
        assert _auto.iter_http() == (sentinel_reg,)
        assert _auto._cli_realized == {"pre": sentinel_reg}
    finally:
        _auto._http.clear()
        _auto._http_keys.clear()
        _auto._cli_realized.clear()


# --- import isolation boundary -------------------------------------------------


def test_retired_casts_cli_module_is_not_shipped():
    assert "lionagi.cli.casts" not in sys.modules
    assert importlib.util.find_spec("lionagi.cli.casts") is None
    assert importlib.util.find_spec("lionagi.casts.surfaces") is not None


def test_auto_module_is_not_exported_from_root():
    import lionagi

    assert "_auto" not in lionagi.__all__
    assert all(target[0] != "_auto" for target in lionagi._LAZY_MAP.values())
