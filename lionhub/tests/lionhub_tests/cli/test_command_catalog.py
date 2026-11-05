# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Contracts for LionHub's CLI command catalog."""

from __future__ import annotations

import argparse
import sys
import types
from contextlib import contextmanager

import pytest

from lionagi import _auto
from lionhub.cli import catalog as _catalog


@contextmanager
def _isolated_registries():
    cache = dict(_catalog._cli_realized)
    _catalog._cli_realized.clear()
    try:
        with _auto._isolated_registry_for_tests():
            yield
    finally:
        _catalog._cli_realized.clear()
        _catalog._cli_realized.update(cache)


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


def test_cli_seeds_are_fixed_21_in_order():
    seeds = _catalog.iter_cli_seeds()
    assert len(seeds) == 21
    actual = tuple((s.name, s.aliases, s.module) for s in seeds)
    assert actual == _EXPECTED_CLI_SEEDS


def test_seed_for_and_command_exists_resolve_canonical_names_and_aliases():
    assert _catalog.seed_for("orchestrate").name == "orchestrate"
    assert _catalog.seed_for("o").name == "orchestrate"
    assert _catalog.seed_for("mon").name == "monitor"
    assert _catalog.seed_for("does-not-exist") is None
    assert _catalog.command_exists("mcp") is True
    assert _catalog.command_exists("does-not-exist") is False


def test_duplicate_seed_token_raises_duplicate_registration_error():
    a = _auto.CliSeed(name="alpha", help="a", module="m.a")
    b = _auto.CliSeed(name="beta", help="b", module="m.b", aliases=("alpha",))
    with pytest.raises(_auto.DuplicateRegistrationError):
        _catalog._build_seed_by_token((a, b))


def test_duplicate_canonical_seed_name_raises_duplicate_registration_error():
    a = _auto.CliSeed(name="same", help="a", module="m.a")
    b = _auto.CliSeed(name="same", help="b", module="m.b")
    with pytest.raises(_auto.DuplicateRegistrationError):
        _catalog._build_seed_by_token((a, b))


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
    monkeypatch.setattr(_catalog, "_cli_seeds", (seed_a, seed_b))
    monkeypatch.setattr(_catalog, "_cli_seed_by_token", {"alpha": seed_a, "beta": seed_b})
    with _isolated_registries():
        registration = _catalog.load_cli_command(seed_a)
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
    monkeypatch.setattr(_catalog, "_cli_seeds", (seed_a,))
    with _isolated_registries():
        first = _catalog.load_cli_command(seed_a)
        second = _catalog.load_cli_command(seed_a)
        assert first is second


def test_cli_zero_matching_markers_raises_contract_error(monkeypatch):
    mod_a = _make_module("tests.contracts._fake_cli_zero", "x = 1\n")
    seed_a = _auto.CliSeed(name="alpha", help="h", module=mod_a.__name__)
    with _isolated_registries():
        with pytest.raises(_auto.RegistrationContractError):
            _catalog.load_cli_command(seed_a)


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
    with _isolated_registries():
        with pytest.raises(_auto.RegistrationContractError):
            _catalog.load_cli_command(seed_a)


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
    with _isolated_registries():
        with pytest.raises(_auto.RegistrationContractError):
            _catalog.load_cli_command(seed_a)


def test_build_cli_parser_with_no_selection_registers_every_seed_as_stub():
    build = _catalog.build_cli_parser(None)
    assert build.parser.prog == "li"
    assert build.seed is None
    assert build.registration is None
    assert build.selected_parser is None
    action_dests = {a.dest for a in build.parser._actions}
    assert "machine" in action_dests
    choices = build.parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert "orchestrate" in choices
    assert "o" in choices  # alias registered
    assert len(_catalog.iter_cli_seeds()) == 21


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
    monkeypatch.setattr(_catalog, "_cli_seeds", (seed_a,))
    monkeypatch.setattr(_catalog, "_cli_seed_by_token", {"alpha": seed_a})
    with _isolated_registries():
        build = _catalog.build_cli_parser(seed_a)
        assert build.seed is seed_a
        assert build.registration.handler is marker_mod.add_alpha_subparser
        assert isinstance(build.selected_parser, argparse.ArgumentParser)
        assert "subparsers" in captured


def test_isolated_cli_registry_restores_previous_state():
    sentinel = _auto.Registration(
        order=0, area="pre", module="m", qualname="q", handler=lambda: None
    )
    _catalog._cli_realized.clear()
    _catalog._cli_realized["pre"] = sentinel
    try:
        with _isolated_registries():
            assert _catalog._cli_realized == {}
            _catalog._cli_realized["inside"] = sentinel
        assert _catalog._cli_realized == {"pre": sentinel}
    finally:
        _catalog._cli_realized.clear()


def test_cli_loader_delegates_to_shared_import_and_keeps_registration(monkeypatch):
    from lionagi import ln

    name = "tests.contracts._shared_import_cli"
    module = _make_module(
        name,
        "from lionagi import _auto\n"
        "@_auto.auto_register(area='shared-import', cli=_auto.CliDeclaration(seed='shared-import', parser_factory=lambda s: None))\n"
        "def handler():\n    return 'handled'\n",
    )
    real_import = ln.import_module
    calls = []

    def shared_import(module_name, *args, **kwargs):
        calls.append((module_name, args, kwargs))
        return real_import(module_name, *args, **kwargs)

    monkeypatch.setattr(ln, "import_module", shared_import)
    with _isolated_registries():
        seed = _auto.CliSeed(name="shared-import", help="h", module=name)
        monkeypatch.setattr(_catalog, "_cli_seeds", (seed,))
        first = _catalog.load_cli_command(seed)
        assert first.handler is module.handler
        assert _catalog.load_cli_command(seed) is first
        assert calls == [(name, (), {})]


@pytest.mark.parametrize("missing", [False, True], ids=["plain-error", "missing-module"])
def test_cli_import_failure_preserves_shared_error_boundary(monkeypatch, tmp_path, missing):
    name = "auto_import_failure_cli"
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
            seed = _auto.CliSeed(name="shared-import", help="h", module=name)
            monkeypatch.setattr(_catalog, "_cli_seeds", (seed,))
            _catalog.load_cli_command(seed)
        assert _catalog._cli_realized == {}
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
