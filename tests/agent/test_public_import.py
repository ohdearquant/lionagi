# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Guards that lionagi.agent imports cleanly and PyYAML stays a core dependency."""

from __future__ import annotations

import importlib
from pathlib import Path

import toml


def test_lionagi_agent_public_surface_imports():
    """``import lionagi.agent`` exposes its documented public surface."""
    mod = importlib.import_module("lionagi.agent")

    assert hasattr(mod, "AgentSpec"), "AgentSpec must be accessible after import"
    assert hasattr(mod, "load_settings"), "load_settings must be accessible after import"
    assert hasattr(mod, "create_agent"), "create_agent must be accessible after import"


def test_pyyaml_is_a_core_dependency():
    """PyYAML must stay in core dependencies, not an optional extra.

    Importing lionagi.agent requires PyYAML at module load time; if it regresses
    to an optional extra, a base install breaks again. This pins the contract.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        import pytest

        pytest.skip("pyproject.toml not available (installed package)")

    data = toml.load(pyproject)
    core_deps = data["project"]["dependencies"]

    assert any(dep.lower().replace("_", "-").startswith("pyyaml") for dep in core_deps), (
        f"pyyaml must be a core dependency, got: {core_deps}"
    )
