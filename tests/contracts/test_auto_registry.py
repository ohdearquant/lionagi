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


def test_retired_casts_cli_module_is_not_shipped():
    assert "lionhub.serve.cli.casts" not in sys.modules
    assert importlib.util.find_spec("lionhub.serve.cli.casts") is None
    assert importlib.util.find_spec("lionhub.agent.casts.surfaces") is not None


def test_auto_module_is_not_exported_from_root():
    import lionagi

    assert "_auto" not in lionagi.__all__
    assert all(target[0] != "_auto" for target in lionagi._LAZY_MAP.values())


@pytest.mark.parametrize(
    "name", ["_HTTP_MODULES", "load_http_modules", "iter_http", "_isolated_registry_for_tests"]
)
def test_the_unused_http_loading_path_is_gone(name):
    assert not hasattr(_auto, name)
