# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lazy import behavior for the agent package."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from lionagi.agent import __all__


def test_import_agent_does_not_load_export_modules_in_a_fresh_process():
    script = textwrap.dedent(
        """
        import sys
        import lionagi.agent

        assert not any(
            name in sys.modules
            for name in (
                "lionagi.agent.factory",
                "lionagi.agent.permissions",
                "lionagi.agent.settings",
                "lionagi.agent.spec",
            )
        )
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.mark.parametrize(
    ("name", "module_name"),
    [
        ("AgentSpec", "spec"),
        ("HooksMixin", "spec"),
        ("PermissionPolicy", "permissions"),
        ("create_agent", "factory"),
        ("load_settings", "settings"),
        ("apply_hooks_from_settings", "settings"),
    ],
)
def test_exports_resolve_to_their_submodule_objects(name: str, module_name: str):
    import importlib

    package = importlib.import_module("lionagi.agent")
    module = importlib.import_module(f"lionagi.agent.{module_name}")

    assert getattr(package, name) is getattr(module, name)


@pytest.mark.parametrize("submodule", ["factory", "gate", "permissions", "settings", "spec"])
def test_submodule_resolves_as_attribute_in_a_fresh_process(submodule: str):
    script = textwrap.dedent(
        f"""
        import importlib
        import sys
        import lionagi.agent

        assert "lionagi.agent.{submodule}" not in sys.modules
        module = lionagi.agent.{submodule}
        assert module is importlib.import_module("lionagi.agent.{submodule}")
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_unknown_attribute_raises_attribute_error():
    import lionagi.agent as agent

    with pytest.raises(AttributeError):
        agent.unknown_export


def test_dir_lists_all_public_exports():
    import lionagi.agent as agent

    assert set(__all__).issubset(dir(agent))
