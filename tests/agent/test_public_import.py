# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for lazy PyYAML import in lionagi.agent.

Issue: importing lionagi.agent transitively triggered a module-level ``import yaml``
in lionagi/agent/settings.py, which raised ModuleNotFoundError on a base install
(PyYAML is an optional dependency).

Fix: the ``yaml`` import is moved inside ``_yaml_safe_load()``, the private helper
called only when a settings.yaml file is actually read.  The public package surface
(``lionagi.agent``) must import cleanly even when PyYAML is absent.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Trust-boundary regression: import must succeed without PyYAML (AGENT-001)
# ---------------------------------------------------------------------------


def test_lionagi_agent_imports_without_pyyaml(monkeypatch):
    """``import lionagi.agent`` succeeds even when PyYAML is not installed.

    Regression guard for the module-level ``import yaml`` that was present in
    lionagi/agent/settings.py.  The import must complete without raising
    ModuleNotFoundError.
    """
    # Simulate PyYAML being absent by blocking it in sys.modules.
    monkeypatch.setitem(sys.modules, "yaml", None)  # type: ignore[arg-type]

    # Drop any cached import so we exercise the actual import machinery.
    for key in list(sys.modules.keys()):
        if key == "lionagi.agent" or key.startswith("lionagi.agent."):
            monkeypatch.delitem(sys.modules, key)

    # This must not raise — the import is the test.
    mod = importlib.import_module("lionagi.agent")

    assert hasattr(mod, "AgentConfig"), "AgentConfig must be accessible after import"
    assert hasattr(mod, "load_settings"), "load_settings must be accessible after import"
    assert hasattr(mod, "create_agent"), "create_agent must be accessible after import"


def test_load_settings_raises_clear_error_when_pyyaml_absent(monkeypatch, tmp_path):
    """``load_settings()`` raises ImportError with an install hint when PyYAML is absent.

    The error must only surface when the yaml-loading function is actually called
    (i.e., when a settings.yaml file exists and is read), not at import time.
    """
    # Create a real settings.yaml so load_settings actually tries to read it.
    settings_dir = tmp_path / ".lionagi"
    settings_dir.mkdir()
    (settings_dir / "settings.yaml").write_text("hooks: {}\n")

    # Block PyYAML so the lazy import inside _yaml_safe_load fails.
    # We also remove the cached yaml module so the import attempt inside
    # _yaml_safe_load actually hits the blocked entry.
    monkeypatch.setitem(sys.modules, "yaml", None)  # type: ignore[arg-type]

    from lionagi.agent.settings import _yaml_safe_load

    with pytest.raises(ImportError, match="PyYAML is required"):
        # Open the yaml file and pass its stream to the helper — simulates
        # exactly the path that load_settings takes when reading settings.yaml.
        with open(settings_dir / "settings.yaml") as f:
            _yaml_safe_load(f)
