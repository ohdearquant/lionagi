# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Guards that lionhub.agent imports cleanly and PyYAML stays a core dependency."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import toml


def test_lionhub_agent_public_surface_imports():
    """``import lionhub.agent`` exposes its documented public surface."""
    mod = importlib.import_module("lionhub.agent")

    assert hasattr(mod, "AgentSpec"), "AgentSpec must be accessible after import"
    assert hasattr(mod, "load_settings"), "load_settings must be accessible after import"
    assert hasattr(mod, "create_agent"), "create_agent must be accessible after import"


def _lionagi_pyproject(start: Path, top: Path | None = None) -> Path | None:
    """Return the lionagi pyproject.toml above ``start``.

    Returns None outside a git checkout (an installed package, where the guard
    cannot run). Inside a checkout a missing file is an error, so moving this
    test never turns the guard into a skip.
    """
    parents = [d for d in start.parents if top is None or d.is_relative_to(top)]
    for directory in parents:
        candidate = directory / "pyproject.toml"
        if candidate.is_file() and toml.load(candidate).get("project", {}).get("name") == "lionagi":
            return candidate
    if any((directory / ".git").exists() for directory in parents):
        raise AssertionError(f"no lionagi pyproject.toml above {start} in a git checkout")
    return None


def test_pyyaml_guard_cannot_skip_inside_a_git_checkout(tmp_path):
    test_file = tmp_path / "repo" / "pkg" / "tests" / "test_x.py"
    test_file.parent.mkdir(parents=True)
    test_file.touch()
    assert _lionagi_pyproject(test_file, tmp_path) is None

    (tmp_path / "repo" / ".git").mkdir()
    with pytest.raises(AssertionError, match="git checkout"):
        _lionagi_pyproject(test_file, tmp_path)

    (tmp_path / "repo" / "pyproject.toml").write_text('[project]\nname = "lionagi"\n')
    assert _lionagi_pyproject(test_file, tmp_path) == tmp_path / "repo" / "pyproject.toml"


def test_pyyaml_is_a_core_dependency():
    """PyYAML must stay in core dependencies, not an optional extra.

    Importing lionhub.agent requires PyYAML at module load time; if it regresses
    to an optional extra, a base install breaks again. This pins the contract.
    """
    pyproject = _lionagi_pyproject(Path(__file__).resolve())
    if pyproject is None:
        pytest.skip("not a source checkout (installed package)")

    data = toml.load(pyproject)
    core_deps = data["project"]["dependencies"]

    assert any(dep.lower().replace("_", "-").startswith("pyyaml") for dep in core_deps), (
        f"pyyaml must be a core dependency, got: {core_deps}"
    )
