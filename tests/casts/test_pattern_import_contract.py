# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Real pattern modules retain canonical loading and precise import failures."""

from __future__ import annotations

import importlib
import sys
from uuid import uuid4

import pytest

from lionagi.casts import pattern


@pytest.fixture
def pattern_sources(tmp_path, monkeypatch):
    package = f"pattern_sources_{uuid4().hex}"
    root = tmp_path / package
    roles = root / "roles"
    modes = roles / "modes"
    modes.mkdir(parents=True)
    for directory in (root, roles, modes):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (root / "state.py").write_text("error = None\n", encoding="utf-8")
    for stem, name in (("zed", "zed"), ("alpha", "alpha"), ("dashed_name", "dashed-name")):
        (roles / f"{stem}.py").write_text(
            "from lionagi.casts.pattern import Role\n"
            f"ROLE = Role(name={name!r}, description='fixture', body='fixture')\n",
            encoding="utf-8",
        )
    for name in ("slow", "fast"):
        (modes / f"{name}.py").write_text(
            "from lionagi.casts.pattern import Mode\n"
            f"MODE = Mode(name={name!r}, description='fixture', behaviors='fixture')\n",
            encoding="utf-8",
        )
    for directory in (roles, modes):
        for name in ("_private", "TEMPLATE"):
            (directory / f"{name}.py").write_text(
                "raise AssertionError('excluded module imported')\n", encoding="utf-8"
            )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(pattern, "_ROLES_PKG", f"{package}.roles")
    monkeypatch.setattr(pattern, "_MODES_PKG", f"{package}.roles.modes")

    def write(stem, source):
        (roles / f"{stem}.py").write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        return f"{package}.roles.{stem}"

    yield package, write
    for name in tuple(sys.modules):
        if name == package or name.startswith(f"{package}."):
            sys.modules.pop(name, None)


def test_cold_leaf_loads_and_sorted_catalogs(pattern_sources):
    package, _write = pattern_sources
    role_module = f"{package}.roles.dashed_name"
    mode_module = f"{package}.roles.modes.fast"
    assert role_module not in sys.modules
    assert mode_module not in sys.modules

    role = pattern.Role.load("dashed-name")
    mode = pattern.Mode.load("fast")

    assert role is sys.modules[role_module].ROLE
    assert mode is sys.modules[mode_module].MODE
    assert pattern.list_roles() == ["alpha", "dashed-name", "zed"]
    assert pattern.list_modes() == ["fast", "slow"]


@pytest.mark.parametrize("name", ["missing", "dashed_name"])
def test_missing_and_noncanonical_roles_keep_unknown_error(pattern_sources, name):
    with pytest.raises(ValueError) as caught:
        pattern.Role.load(name)

    assert str(caught.value) == (f"Unknown role: {name!r}. Available: alpha, dashed-name, zed")


def test_missing_dependency_preserves_original_exception(pattern_sources):
    package, write = pattern_sources
    dependency = f"missing_dependency_{uuid4().hex}"
    target = write(
        "dependency_failure",
        f"from {package} import state\n"
        "try:\n"
        f"    import {dependency}\n"
        "except ModuleNotFoundError as exc:\n"
        "    state.error = exc\n"
        "    raise\n",
    )

    with pytest.raises(ModuleNotFoundError) as caught:
        pattern.Role.load("dependency-failure")

    state = sys.modules[f"{package}.state"]
    assert caught.value is state.error
    assert caught.value.name == dependency
    assert caught.value.name != target
    assert caught.value.__cause__ is None


def test_other_import_error_has_module_prefix_and_original_cause(pattern_sources):
    package, write = pattern_sources
    target = write(
        "import_failure",
        f"from {package} import state\n"
        "try:\n"
        "    raise ImportError('fixture initialization refused')\n"
        "except ImportError as exc:\n"
        "    state.error = exc\n"
        "    raise\n",
    )

    with pytest.raises(ImportError) as caught:
        pattern.Role.load("import-failure")

    assert type(caught.value) is ImportError
    assert str(caught.value) == f"Failed to import module {target}: fixture initialization refused"
    assert caught.value.__cause__ is sys.modules[f"{package}.state"].error
    assert caught.value is not caught.value.__cause__
